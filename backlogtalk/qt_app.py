#!/usr/bin/env python3
"""BacklogTalk Qt desktop interface."""

from __future__ import annotations

import importlib.metadata
import itertools
import json
import os
import platform
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
import wave
from functools import partial
from pathlib import Path

from PySide6.QtCore import (
    QDate,
    QItemSelectionModel,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    qVersion,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSizeGrip,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from backlogtalk import core, shortcuts
from backlogtalk.audio_io import (
    MeetingRecorder,
    MixJob,
    VoiceRecorder,
    cleanup_interrupted_import,
    create_mix_job,
    finish_mix,
    import_media,
    mix_meeting,
    read_mix_job,
    system_output_device,
)
from backlogtalk.epic_manager import EpicManager
from backlogtalk.instance import Instance
from backlogtalk.qt_widgets import (
    STYLE,
    AudioWave,
    CapturePopup,
    ClickComboBox,
    Header,
    SelectableList,
    button,
    label,
    navigation_icon,
    recording_tray_icon,
    tray_icon,
)
from backlogtalk.version import VERSION


def transcription_worker(jobs, results):
    while True:
        _, _, capture_id, path, model, *options = jobs.get()
        language = options[0] if options else "es"
        cancel = options[1] if len(options) > 1 else None
        if capture_id is None:
            return
        operation = None
        try:
            source = path.output if isinstance(path, MixJob) else path
            operation = core.begin_operation(
                "transcripción", capture_id, f"Audio: {source}\nModelo: {model}\nIdioma: {language}"
            )
            if isinstance(path, MixJob):
                results.put(("mix_start", capture_id, None))
                try:
                    job = path
                    path = mix_meeting(
                        job,
                        progress=lambda pos, total, elapsed, capture=capture_id: results.put(
                            ("mix_progress", capture, (pos, total, elapsed))
                        ),
                    )
                    finish_mix(job)
                except Exception as error:
                    results.put(
                        (
                            "mix_error",
                            capture_id,
                            {
                                "message": str(error),
                                "details": f"Audio: {source}\nModelo: {model}\n"
                                + traceback.format_exc(),
                                "operation": operation,
                            },
                        )
                    )
                    continue
            results.put(("asr_start", capture_id, None))
            text = core.transcribe(
                path,
                model,
                progress=lambda pos, total, elapsed, capture=capture_id: results.put(
                    ("asr_progress", capture, (pos, total, elapsed))
                ),
                language=language,
                cancel=cancel,
            )
            if cancel is not None and cancel.is_set():
                raise core.TranscriptionCancelled()
            results.put(("asr_done", capture_id, {"text": text, "operation": operation}))
        except core.TranscriptionCancelled:
            results.put(("asr_cancelled", capture_id, {"operation": operation}))
        except Exception as error:
            results.put(
                (
                    "asr_error",
                    capture_id,
                    {
                        "message": str(error),
                        "details": f"Audio: {source}\nModelo: {model}\n" + traceback.format_exc(),
                        "operation": operation,
                    },
                )
            )


class VoiceWindow(QWidget):
    def __init__(self, instance=None):
        super().__init__()
        self.setWindowTitle("BacklogTalk")
        self.setWindowIcon(QIcon(str(core.ROOT / "assets/backlogtalk-mark.svg")))
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(1060, 740)
        self.setMinimumSize(900, 620)
        self.instance = instance
        self.config = core.load_config()
        if QKeySequence(self.config.get("shortcut", "Ctrl+Alt+J")) == QKeySequence(
            self.config.get("shortcut_system", "Ctrl+Alt+K")
        ):
            self.config["shortcut_system"] = (
                "Ctrl+Alt+L"
                if QKeySequence(self.config.get("shortcut")) == QKeySequence("Ctrl+Alt+K")
                else "Ctrl+Alt+K"
            )
        used_shortcuts = {
            QKeySequence(self.config.get("shortcut", "Ctrl+Alt+J")).toString(),
            QKeySequence(self.config.get("shortcut_system", "Ctrl+Alt+K")).toString(),
        }
        if (
            QKeySequence(self.config.get("shortcut_open", "Ctrl+Alt+B")).toString()
            in used_shortcuts
        ):
            self.config["shortcut_open"] = next(
                key
                for key in ("Ctrl+Alt+B", "Ctrl+Alt+L", "Ctrl+Alt+O")
                if QKeySequence(key).toString() not in used_shortcuts
            )
        self.recorder = VoiceRecorder()
        self.recording = False
        self.recording_stopping = False
        self.record_confirmation_pending = False
        self.recording_source = None
        self.audio_write_error_reported = False
        self.importing = False
        self.capture_id = None
        self.review_id = None
        self.busy = False
        self.migrating = False
        self.playing = False
        self.play_cancel = None
        self.close_after_playback = False
        self.close_after_recording = False
        self.closing = False
        self.quick_recording = False
        self.quick_context = None
        self.quick_id = None
        self.record_operation = None
        self.background_context = None
        self.recovery_checked = False
        self.failure_windows = set()
        self.deferred_failures = []
        self.notice_windows = set()
        self.tray = None
        self.quit_requested = False
        self.capture_only = False
        self.tray_blink_on = False
        self.tray_blink_timer = QTimer(self)
        self.tray_blink_timer.setInterval(650)
        self.tray_blink_timer.timeout.connect(self.blink_tray)
        self.popup = CapturePopup(self)
        self.shortcut_lookup_running = False
        self.jobs = queue.PriorityQueue()
        self.results = queue.Queue()
        self.order = itertools.count()
        self.pending = set()
        self.cancel_events = {}
        self.manual_cancel = None
        self.manual_capture_id = None
        self.running_id = None
        self.mixing_id = None
        self.mix_progress = None
        self.import_progress = None
        self.asr_progress = None
        self.manual_progress = None
        self.manual_transcribing = False
        self.asr_errors = {}
        self.skill_saved = ""
        self.transcription_thread = threading.Thread(
            target=transcription_worker, args=(self.jobs, self.results), daemon=True
        )
        self.transcription_thread.start()
        self._build()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(120)
        try:
            import psutil

            self.resource_system = psutil
            psutil.cpu_percent(None)
        except Exception:
            self.resource_system = None
        self.resource_last_sample = 0.0
        self.resource_timer = QTimer(self)
        self.resource_timer.setInterval(1000)
        self.resource_timer.timeout.connect(self.update_resources)
        self.resource_timer.start()
        self.activity_timer = QTimer(self)
        self.activity_timer.setInterval(10000)
        self.activity_timer.timeout.connect(self.update_daily_activity)
        self.activity_timer.start()
        self.update_daily_activity()
        self.shortcut = QShortcut(QKeySequence(self.config.get("shortcut", "Ctrl+Alt+J")), self)
        self.shortcut.activated.connect(self.shortcut_capture)
        self.system_shortcut = QShortcut(
            QKeySequence(self.config.get("shortcut_system", "Ctrl+Alt+K")), self
        )
        self.system_shortcut.activated.connect(lambda: self.shortcut_capture("system"))
        self.open_shortcut = QShortcut(
            QKeySequence(self.config.get("shortcut_open", "Ctrl+Alt+B")), self
        )
        self.open_shortcut.activated.connect(self.shortcut_open_requested)
        for shortcut in (self.shortcut, self.system_shortcut, self.open_shortcut):
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.settings_dirty = False
        self.settings_timer = QTimer(self)
        self.settings_timer.setSingleShot(True)
        self.settings_timer.setInterval(800)
        self.settings_timer.timeout.connect(lambda: self.save_settings(notify=False))
        self.skill_timer = QTimer(self)
        self.skill_timer.setSingleShot(True)
        self.skill_timer.setInterval(800)
        self.skill_timer.timeout.connect(lambda: self.save_skill(notify=False))
        for entry in self.settings.values():
            entry.textChanged.connect(self.schedule_settings_save)
        for combo in (self.whisper_model, self.transcription_language, self.backend, self.mics):
            combo.currentIndexChanged.connect(self.schedule_settings_save)
        self.cursor_model.currentTextChanged.connect(self.schedule_settings_save)
        self.shortcut_edit.textChanged.connect(self.schedule_settings_save)
        self.shortcut_system_edit.textChanged.connect(self.schedule_settings_save)
        self.shortcut_open_edit.textChanged.connect(self.schedule_settings_save)
        self.skill.textChanged.connect(self.schedule_skill_save)
        self.shortcut_refresh_timer = QTimer(self)
        self.shortcut_refresh_timer.setInterval(10000)
        self.shortcut_refresh_timer.timeout.connect(self.refresh_shortcut_summary)
        self.shortcut_refresh_timer.start()
        self.refresh_shortcut_summary()
        QTimer.singleShot(250, self.resume_pending)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 22, 22)
        painter.fillPath(path, QColor("#151c24"))
        painter.setPen(QColor("#394755"))
        painter.drawPath(path)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 11, 14, 11)
        outer.setSpacing(10)
        outer.addWidget(Header(self))
        body = QHBoxLayout()
        body.setSpacing(20)
        outer.addLayout(body, 1)
        rail = QFrame()
        rail.setObjectName("sidebar")
        rail.setFixedWidth(190)
        sidebar = QVBoxLayout(rail)
        sidebar.setContentsMargins(8, 12, 8, 12)
        sidebar.setSpacing(3)
        body.addWidget(rail)
        section = label("ESPACIO DE TRABAJO", "eyebrow")
        section.setContentsMargins(10, 3, 0, 10)
        sidebar.addWidget(section)
        self.nav = {}
        for text, page in [("Capturar", 0), ("Tickets", 1), ("Historial", 2), ("Configuración", 3)]:
            b = button(text, lambda _checked=False, p=page: self.show_page(p), "nav")
            b.setCheckable(True)
            b.setIcon(navigation_icon(page))
            b.setIconSize(QSize(18, 18))
            self.nav[page] = b
            sidebar.addWidget(b)
        sidebar.addStretch()
        shortcut = label("ATAJOS", "eyebrow")
        shortcut.setContentsMargins(10, 0, 0, 0)
        sidebar.addWidget(shortcut)
        self.shortcut_value = label("Consultando el sistema…", "muted")
        sidebar.addWidget(self.shortcut_value)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        self._build_capture()
        self._build_tickets()
        self._build_history()
        self._build_settings()
        self._build_review()
        self._build_skill()
        footer = QHBoxLayout()
        self.status = label("Lista para capturar una idea.", "muted")
        self.status.setWordWrap(False)
        self.status.setMinimumWidth(0)
        footer.addWidget(self.status, 1)
        self.system_resource_label = label("CPU -- | RAM --", "muted")
        self.system_resource_label.setWordWrap(False)
        self.system_resource_label.setToolTip("Uso total de CPU y memoria del equipo.")
        footer.addWidget(self.system_resource_label)
        self.daily_activity_label = label("Jira hoy -- | Tokens --", "muted")
        self.daily_activity_label.setWordWrap(False)
        self.daily_activity_label.setToolTip(
            "Tickets creados y verificados por BacklogTalk desde que se activó este contador. "
            "Los tokens son solo los reportados por Ollama a esta instalación; Cursor no los informa en la salida utilizada."
        )
        footer.addWidget(self.daily_activity_label)
        version = label(f"v{VERSION}", "muted")
        version.setWordWrap(False)
        version.setToolTip("Versión de BacklogTalk")
        footer.addWidget(version)
        grip = QSizeGrip(self)
        grip.setFixedSize(18, 18)
        footer.addWidget(grip)
        outer.addLayout(footer)
        self.progress_panel = QWidget()
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(5)
        progress_row = QHBoxLayout()
        self.progress_label = label("", "muted")
        self.progress_label.setWordWrap(False)
        progress_row.addWidget(self.progress_label, 1)
        self.cancel_transcription_button = button(
            "Cancelar transcripción", self.cancel_transcription
        )
        self.cancel_transcription_button.hide()
        progress_row.addWidget(self.cancel_transcription_button)
        progress_layout.addLayout(progress_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        progress_layout.addWidget(self.progress)
        outer.addWidget(self.progress_panel)
        self.progress_panel.hide()
        self.show_page(0)

    def _page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)
        layout.addWidget(label(title, "title"))
        layout.addWidget(label(subtitle, "muted"))
        card = QFrame()
        card.setObjectName("card")
        inside = QVBoxLayout(card)
        inside.setContentsMargins(18, 15, 18, 15)
        inside.setSpacing(9)
        layout.addWidget(card, 1)
        self.pages.addWidget(page)
        return inside

    def _build_capture(self):
        box = self._page(
            "Capturar", "Grabá una idea ahora. Revisala y prepará el ticket cuando quieras."
        )
        box.setSpacing(13)
        top = QHBoxLayout()
        top.setSpacing(12)
        box.addLayout(top)
        capture = QFrame()
        capture.setObjectName("recordSurface")
        record = QVBoxLayout(capture)
        record.setContentsMargins(16, 14, 16, 14)
        record.setSpacing(9)
        header = QHBoxLayout()
        header.addWidget(label("FUENTE", "eyebrow"))
        header.addStretch()
        self.source = ClickComboBox()
        self.source.setObjectName("sourceSelector")
        self.source.addItem("Micrófono", "mic")
        self.source.addItem("Mic + salida", "system")
        self.source.addItem("Archivo…", "file")
        self.source.setMaxVisibleItems(3)
        self.source.view().setMinimumHeight(120)
        self.source.view().setMinimumWidth(180)
        self.source.setToolTip("Elegí qué querés capturar")
        header.addWidget(self.source)
        record.addLayout(header)
        middle = QHBoxLayout()
        middle.setSpacing(10)
        self.record_button = button("●  Grabar nota", self.toggle_record, "record")
        self.record_button.setToolTip("Iniciar o detener y guardar la grabación")
        self.record_button.setMinimumHeight(38)
        middle.addWidget(self.record_button)
        self.cancel_record_button = button("Cancelar", self.cancel_record)
        self.cancel_record_button.hide()
        middle.addWidget(self.cancel_record_button)
        self.waveform = AudioWave()
        self.source_effect = QGraphicsOpacityEffect(self.waveform)
        self.waveform.setGraphicsEffect(self.source_effect)
        self.source_animation = QPropertyAnimation(self.source_effect, b"opacity", self)
        self.source_animation.setDuration(220)
        middle.addWidget(self.waveform, 1)
        record.addLayout(middle)
        self.record_hint = label("Se guarda y transcribe automáticamente.", "muted")
        record.addWidget(self.record_hint)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.hide()
        record.addWidget(self.meter)
        self.source.currentIndexChanged.connect(self.source_changed)
        top.addWidget(capture, 5)
        destination = QFrame()
        destination.setObjectName("surface")
        dest = QVBoxLayout(destination)
        dest.setContentsMargins(16, 14, 16, 14)
        dest.setSpacing(9)
        dest.addWidget(label("DESTINO DEL TICKET", "eyebrow"))
        row = QHBoxLayout()
        self.project = ClickComboBox()
        self.project.setEditable(True)
        self.project.addItems(self.config.get("projects", []))
        self.project.setCurrentText(self.config.get("last_project", ""))
        self.project.setFixedWidth(100)
        self.epic = ClickComboBox()
        self.epic.setEditable(True)
        self.project.currentTextChanged.connect(self.project_changed)
        row.addWidget(self.project)
        row.addWidget(self.epic, 1)
        manage_epics = button("Gestionar", self.manage_epics)
        manage_epics.setToolTip("Buscar y elegir las épicas del proyecto")
        row.addWidget(manage_epics)
        dest.addLayout(row)
        self.fill_epics()
        dest.addWidget(label("Podés cambiarlo antes de enviar.", "muted"))
        top.addWidget(destination, 6)
        editor = QFrame()
        editor.setObjectName("surface")
        edit = QVBoxLayout(editor)
        edit.setContentsMargins(16, 14, 16, 14)
        edit.setSpacing(10)
        box.addWidget(editor, 1)
        row = QHBoxLayout()
        row.addWidget(label("Nota actual", "sectionTitle"))
        row.addStretch()
        row.addWidget(label("EDICIÓN LOCAL", "eyebrow"))
        edit.addLayout(row)
        self.note = QTextEdit()
        self.note.setPlaceholderText(
            "Tu transcripción aparecerá acá. También podés escribir una nota…"
        )
        edit.addWidget(self.note, 1)
        row = QHBoxLayout()
        row.addWidget(button("Guardar y crear otra", self.new_capture))
        row.addStretch()
        row.addWidget(button("Preparar ticket  →", self.prepare, "primary"))
        edit.addLayout(row)

    def _build_tickets(self):
        box = self._page(
            "Mis tickets",
            "Doble clic para revisar. Ctrl o Shift + clic selecciona varios; Supr los borra de la lista local.",
        )
        self.tickets = SelectableList()
        self.tickets.itemDoubleClicked.connect(
            lambda _: (
                self.open_ticket()
                if QApplication.keyboardModifiers() == Qt.KeyboardModifier.NoModifier
                else None
            )
        )
        self.tickets.delete_requested.connect(self.delete_tickets)
        box.addWidget(self.tickets, 1)
        row = QHBoxLayout()
        row.addWidget(button("Revisar ticket", self.open_ticket))
        self.open_jira_button = button("Abrir en Jira", self.open_selected_jira_ticket)
        self.open_jira_button.setEnabled(False)
        self.tickets.itemSelectionChanged.connect(self.update_open_jira_button)
        row.addWidget(self.open_jira_button)
        row.addStretch()
        row.addWidget(button("Borrar selección", self.delete_tickets, "danger"))
        box.addLayout(row)

    def _build_history(self):
        box = self._page(
            "Historial de notas",
            "Ctrl o Shift + clic selecciona varias notas; Supr borra la selección.",
        )
        self.history = SelectableList()
        self.history.itemDoubleClicked.connect(lambda _: self.open_capture())
        self.history.delete_requested.connect(self.delete_captures)
        box.addWidget(self.history, 1)
        row = QHBoxLayout()
        row.addWidget(button("Abrir nota", self.open_capture))
        self.play_button = button("Escuchar audio", self.play_audio)
        row.addWidget(self.play_button)
        row.addWidget(button("Transcribir audio", self.retranscribe))
        self.error_details_button = button("Ver error", self.show_selected_error)
        self.error_details_button.hide()
        row.addWidget(self.error_details_button)
        self.history.itemSelectionChanged.connect(self.update_error_details_button)
        row.addStretch()
        row.addWidget(button("Borrar selección", self.delete_captures, "danger"))
        box.addLayout(row)
        self.history_location = label(f"Guardado local: {core.APP_HOME}", "muted")
        box.addWidget(self.history_location)

    def _build_settings(self):
        box = self._page("Configuración", "Elegí los modelos para transcribir y preparar tickets.")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: #151c24; border: 0; }")
        scroll.viewport().setStyleSheet("background: #151c24;")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("settingsContent")
        content.setStyleSheet("#settingsContent { background: #151c24; }")
        form = QVBoxLayout(content)
        form.setContentsMargins(0, 0, 12, 0)
        form.setSpacing(8)
        scroll.setWidget(content)
        box.addWidget(scroll, 1)
        self.settings = {}
        for key, title in [
            ("projects", "Proyectos, separados por coma"),
            ("jira_site", "Sitio Jira"),
        ]:
            row = QHBoxLayout()
            text = label(title)
            text.setMinimumWidth(165)
            row.addWidget(text)
            value = (
                ", ".join(self.config.get(key, []))
                if key == "projects"
                else str(self.config.get(key, ""))
            )
            entry = QLineEdit(value)
            self.settings[key] = entry
            row.addWidget(entry, 1)
            form.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button("Sincronizar campos de Jira", self.sync_project_fields))
        self.jira_sync_status = label(
            core.jira_cache_status(
                self.config.get("jira_site", ""), self.config.get("projects", [])
            ),
            "muted",
        )
        row.addWidget(self.jira_sync_status, 1)
        form.addLayout(row)
        row = QHBoxLayout()
        text = label("Transcripción / Whisper")
        text.setMinimumWidth(165)
        row.addWidget(text)
        self.whisper_model = ClickComboBox()
        self.whisper_model.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.whisper_model.setMinimumContentsLength(12)
        for model, description in [
            ("tiny", "Muy rápido / menor precisión"),
            ("base", "Rápido"),
            ("small", "Equilibrado"),
            ("medium", "Más preciso / más lento"),
            ("large-v3", "Mayor tamaño / más lento"),
            ("turbo", "Grande / optimizado"),
        ]:
            self.whisper_model.addItem(f"{model} - {description}", model)
        selected = self.config.get("whisper_model", "small")
        index = self.whisper_model.findData(selected)
        if index < 0:
            self.whisper_model.addItem(f"{selected} - Personalizado", selected)
            index = self.whisper_model.count() - 1
        self.whisper_model.setCurrentIndex(index)
        row.addWidget(self.whisper_model, 1)
        form.addLayout(row)
        row = QHBoxLayout()
        text = label("Idioma del audio")
        text.setMinimumWidth(165)
        row.addWidget(text)
        self.transcription_language = ClickComboBox()
        self.transcription_language.addItem("Español", "es")
        self.transcription_language.addItem("Inglés", "en")
        lang_index = self.transcription_language.findData(
            self.config.get("transcription_language", "es")
        )
        self.transcription_language.setCurrentIndex(max(0, lang_index))
        row.addWidget(self.transcription_language, 1)
        form.addLayout(row)
        help_text = label(
            "Whisper (faster-whisper) procesa el audio localmente. Al usar un modelo por primera vez se descarga desde Hugging Face.",
            "muted",
        )
        form.addWidget(help_text)
        row = QHBoxLayout()
        caption = label("Motor para preparar tickets")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        self.backend = ClickComboBox()
        self.backend.addItems(["cursor", "ollama"])
        self.backend.setCurrentText(self.config.get("model_backend", "cursor"))
        row.addWidget(self.backend, 1)
        form.addLayout(row)
        self.cursor_row = QWidget()
        row = QHBoxLayout(self.cursor_row)
        row.setContentsMargins(0, 0, 0, 0)
        caption = label("Modelo de Cursor")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        self.cursor_model = ClickComboBox()
        self.cursor_model.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.cursor_model.setMinimumContentsLength(12)
        self.cursor_model.setEditable(True)
        self.cursor_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.cursor_model.addItem("")
        self.cursor_model.lineEdit().setPlaceholderText("Predeterminado de Cursor CLI")
        self.cursor_model.setCurrentText(self.config.get("cursor_model", ""))
        row.addWidget(self.cursor_model, 1)
        self.model_list_button = button("Cargar modelos", self.load_cursor_models)
        row.addWidget(self.model_list_button)
        form.addWidget(self.cursor_row)
        self.ollama_row = QWidget()
        row = QHBoxLayout(self.ollama_row)
        row.setContentsMargins(0, 0, 0, 0)
        caption = label("Modelo local de Ollama")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        self.settings["ollama_model"] = QLineEdit(str(self.config.get("ollama_model", "")))
        row.addWidget(self.settings["ollama_model"], 1)
        form.addWidget(self.ollama_row)
        self.ollama_help = label(
            "Solo se usa cuando el motor es Ollama; debe estar instalado en tu equipo.", "muted"
        )
        form.addWidget(self.ollama_help)
        self.backend.currentTextChanged.connect(self.update_backend_fields)
        self.update_backend_fields()
        row = QHBoxLayout()
        caption = label("Instrucciones")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        row.addWidget(button("Gestionar instrucciones…", self.edit_jira_context))
        row.addStretch()
        form.addLayout(row)
        form.addWidget(
            label("Formato general y reglas para cada proyecto o épica de Jira.", "muted")
        )
        row = QHBoxLayout()
        caption = label("Micrófono")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        self.mics = ClickComboBox()
        self.mics.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.mics.setMinimumContentsLength(18)
        self.mics.addItem("Predeterminado del sistema", None)
        try:
            import sounddevice as sd

            for index, device in enumerate(sd.query_devices()):
                if device["max_input_channels"] > 0:
                    host = sd.query_hostapis(device["hostapi"])["name"]
                    self.mics.addItem(f"{device['name']} ({host}, {index})", index)
        except Exception:
            pass
        mic_index = self.mics.findText(
            self.config.get("audio_device", "Predeterminado del sistema")
        )
        if mic_index >= 0:
            self.mics.setCurrentIndex(mic_index)
        row.addWidget(self.mics, 1)
        form.addLayout(row)
        form.addSpacing(8)
        form.addWidget(label("ATAJOS DE LA APLICACIÓN", "eyebrow"))
        row = QHBoxLayout()
        caption = label("Micrófono (app)")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        self.shortcut_stack = QStackedWidget()
        self.shortcut_edit = QLineEdit(self.config.get("shortcut", "Ctrl+Alt+J"))
        self.shortcut_edit.setPlaceholderText("Ej.: Ctrl+Alt+J")
        self.shortcut_keys = QKeySequenceEdit()
        self.shortcut_keys.setMaximumSequenceLength(1)
        self.shortcut_keys.keySequenceChanged.connect(self.shortcut_keys_changed)
        self.shortcut_stack.addWidget(self.shortcut_edit)
        self.shortcut_stack.addWidget(self.shortcut_keys)
        row.addWidget(self.shortcut_stack, 1)
        self.shortcut_mode = button("Pulsar teclas", self.toggle_shortcut_mode)
        row.addWidget(self.shortcut_mode)
        form.addLayout(row)
        self.shortcut_help = label("Escribí la combinación o elegí «Pulsar teclas».", "muted")
        form.addWidget(self.shortcut_help)
        row = QHBoxLayout()
        caption = label("Reunión (app)")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        self.shortcut_system_stack = QStackedWidget()
        self.shortcut_system_edit = QLineEdit(self.config.get("shortcut_system", "Ctrl+Alt+K"))
        self.shortcut_system_edit.setPlaceholderText("Ej.: Ctrl+Alt+K")
        self.shortcut_system_keys = QKeySequenceEdit()
        self.shortcut_system_keys.setMaximumSequenceLength(1)
        self.shortcut_system_keys.keySequenceChanged.connect(self.shortcut_system_keys_changed)
        self.shortcut_system_stack.addWidget(self.shortcut_system_edit)
        self.shortcut_system_stack.addWidget(self.shortcut_system_keys)
        row.addWidget(self.shortcut_system_stack, 1)
        self.shortcut_system_mode = button("Pulsar teclas", self.toggle_system_shortcut_mode)
        row.addWidget(self.shortcut_system_mode)
        form.addLayout(row)
        row = QHBoxLayout()
        caption = label("Abrir historial (app)")
        caption.setMinimumWidth(165)
        row.addWidget(caption)
        self.shortcut_open_stack = QStackedWidget()
        self.shortcut_open_edit = QLineEdit(self.config.get("shortcut_open", "Ctrl+Alt+B"))
        self.shortcut_open_edit.setPlaceholderText("Ej.: Ctrl+Alt+B")
        self.shortcut_open_keys = QKeySequenceEdit()
        self.shortcut_open_keys.setMaximumSequenceLength(1)
        self.shortcut_open_keys.keySequenceChanged.connect(self.shortcut_open_keys_changed)
        self.shortcut_open_stack.addWidget(self.shortcut_open_edit)
        self.shortcut_open_stack.addWidget(self.shortcut_open_keys)
        row.addWidget(self.shortcut_open_stack, 1)
        self.shortcut_open_mode = button("Pulsar teclas", self.toggle_open_shortcut_mode)
        row.addWidget(self.shortcut_open_mode)
        form.addLayout(row)
        form.addWidget(
            label(
                "Para usar atajos desde otras aplicaciones, configuralos también en el sistema.",
                "muted",
            )
        )
        row = QHBoxLayout()
        row.addWidget(button("Configurar atajo global", self.global_shortcut_help))
        row.addStretch()
        form.addLayout(row)
        form.addSpacing(8)
        form.addWidget(label("ARCHIVOS LOCALES", "eyebrow"))
        row = QHBoxLayout()
        row.addWidget(button("Abrir datos", self.open_storage))
        row.addWidget(button("Abrir configuración", self.open_configuration))
        row.addWidget(button("Abrir caché de modelos", self.open_model_cache))
        row.addWidget(button("Mover datos…", self.move_storage))
        row.addStretch()
        form.addLayout(row)
        form.addWidget(
            label(
                "Los modelos Whisper descargados usan la caché de Hugging Face y no se mueven con estos datos.",
                "muted",
            )
        )
        form.addWidget(button("Acerca de BacklogTalk", self.show_about))
        form.addStretch()
        box.addWidget(label("Los cambios se guardan automáticamente.", "muted"))

    def update_backend_fields(self, *_):
        cursor = self.backend.currentText() == "cursor"
        self.cursor_row.setVisible(cursor)
        self.ollama_row.setVisible(not cursor)
        self.ollama_help.setVisible(not cursor)

    def load_cursor_models(self):
        def loaded(result):
            models, _current = result
            previous = self.cursor_model.currentText().strip()
            self.cursor_model.clear()
            self.cursor_model.addItem("")
            self.cursor_model.addItems(models)
            selected = "composer-2.5" if "composer-2.5" in models else previous
            self.cursor_model.setCurrentText(selected)
            self.status.setText(
                "Composer 2.5 seleccionado; se guardará automáticamente."
                if "composer-2.5" in models
                else "Composer 2.5 no está disponible; se conservó la selección anterior."
            )

        self.background(core.list_cursor_models, loaded, "Consultando modelos de Cursor…")

    @staticmethod
    def update_shortcut_editor(editor, sequence):
        text = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if text:
            editor.setText(text)

    def toggle_shortcut_editor(self, stack, editor, keys, mode):
        if stack.currentIndex() == 0:
            sequence = QKeySequence.fromString(
                editor.text().strip(), QKeySequence.SequenceFormat.PortableText
            )
            keys.setKeySequence(sequence)
            stack.setCurrentIndex(1)
            mode.setText("Escribir")
            self.shortcut_help.setText(
                "Pulsá la combinación. Si el sistema la reserva, elegí «Escribir»."
            )
            keys.setFocus()
        else:
            stack.setCurrentIndex(0)
            mode.setText("Pulsar teclas")
            self.shortcut_help.setText("Escribí la combinación o elegí «Pulsar teclas».")
            editor.setFocus()

    def shortcut_keys_changed(self, sequence):
        self.update_shortcut_editor(self.shortcut_edit, sequence)

    def toggle_shortcut_mode(self):
        self.toggle_shortcut_editor(
            self.shortcut_stack, self.shortcut_edit, self.shortcut_keys, self.shortcut_mode
        )

    def shortcut_system_keys_changed(self, sequence):
        self.update_shortcut_editor(self.shortcut_system_edit, sequence)

    def toggle_system_shortcut_mode(self):
        self.toggle_shortcut_editor(
            self.shortcut_system_stack,
            self.shortcut_system_edit,
            self.shortcut_system_keys,
            self.shortcut_system_mode,
        )

    def shortcut_open_keys_changed(self, sequence):
        self.update_shortcut_editor(self.shortcut_open_edit, sequence)

    def toggle_open_shortcut_mode(self):
        self.toggle_shortcut_editor(
            self.shortcut_open_stack,
            self.shortcut_open_edit,
            self.shortcut_open_keys,
            self.shortcut_open_mode,
        )

    def shortcut_editor_focused(self):
        focus = QApplication.focusWidget()
        if not self.isActiveWindow() or focus is None:
            return False
        return any(
            stack.isVisible() and (focus == stack or stack.isAncestorOf(focus))
            for stack in (self.shortcut_stack, self.shortcut_system_stack, self.shortcut_open_stack)
        )

    def update_storage_paths(self):
        if hasattr(self, "history_location"):
            self.history_location.setText(f"Guardado local: {core.APP_HOME}")
        if hasattr(self, "skill_location"):
            self.skill_location.setText(f"Archivo personal: {core.custom_skill_path()}")

    def open_storage(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(core.APP_HOME)))

    def open_configuration(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(core.CONFIG_DIR)))

    def open_model_cache(self):
        try:
            from huggingface_hub.constants import HF_HUB_CACHE

            path = Path(HF_HUB_CACHE)
            if not path.is_dir():
                self.info(
                    "Caché de modelos",
                    "Todavía no existe una caché de Hugging Face en este equipo.",
                )
                return
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self.error("No se pudo abrir", "El sistema no pudo abrir la caché de modelos.")
        except ImportError:
            self.error("Caché no disponible", "No se encontró la biblioteca de Hugging Face.")

    def move_storage(self):
        if self.recording or self.pending or self.busy or self.manual_transcribing:
            self.info(
                "Trabajo en curso",
                "Esperá a que terminen las grabaciones y transcripciones antes de mover los datos.",
            )
            return
        parent = QFileDialog.getExistingDirectory(
            self, "Elegí la carpeta donde crear BacklogTalk-data", str(Path.home())
        )
        if not parent:
            return
        destination = Path(parent) / "BacklogTalk-data"
        if self.settings_dirty and not self.save_settings():
            return
        if not self.confirm(
            "Mover datos locales",
            f"Se copiarán y verificarán los datos en:\n{destination}\n\nLos originales quedarán como respaldo. Los modelos Whisper y los audios externos a la carpeta de datos no se mueven.",
        ):
            return
        if self.note.toPlainText().strip() and self.save_note() is None:
            return
        self.migrating = True
        self.update_progress()
        self.pages.setEnabled(False)
        for nav in self.nav.values():
            nav.setEnabled(False)

        def done(root):
            self.update_storage_paths()
            self.refresh()
            self.status.setText("Datos copiados y nueva ubicación activada.")
            self.info(
                "Datos trasladados",
                f"Nueva ubicación: {root}\n\nLos originales se conservaron como respaldo.",
            )

        self.background(
            lambda: core.relocate_storage(Path(parent)),
            done,
            "Copiando y verificando datos locales…",
        )

    def global_shortcut_help(self):
        if sys.platform.startswith("linux"):
            command = shlex.quote(str(core.ROOT / "scripts/run.sh")) + " --capture"
            message = (
                "Configurá atajos personalizados en el escritorio. Funcionan con la app cerrada.\n\nMicrófono: "
                + command
                + "\n\nReunión (micrófono y equipo): "
                + shlex.quote(str(core.ROOT / "scripts/run.sh"))
                + " --capture-system"
                + "\n\nAbrir aplicación e historial: "
                + shlex.quote(str(core.ROOT / "scripts/run.sh"))
                + " --open-history"
            )
            self.info("Atajo global de Linux", message)
            if (
                os.environ.get("XDG_CURRENT_DESKTOP", "").lower().find("gnome") >= 0
                or "ubuntu" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            ):
                try:
                    subprocess.Popen(["gnome-control-center", "keyboard"])
                except OSError:
                    pass
        elif sys.platform == "darwin":
            command = (
                shlex.quote(str(core.ROOT / "scripts/run.sh")) + " --capture > /dev/null 2>&1 &"
            )
            self.info(
                "Atajo global de macOS",
                "Creá una acción rápida para micrófono con este comando. La captura de audio del equipo aún no está disponible en macOS.\n\n"
                + command
                + "\n\nPara abrir la aplicación y el historial: "
                + shlex.quote(str(core.ROOT / "scripts/run.sh"))
                + " --open-history > /dev/null 2>&1 &",
            )
            QDesktopServices.openUrl(
                QUrl(
                    "https://support.apple.com/guide/shortcuts-mac/run-a-shortcut-while-working-on-your-mac-apd163eb9f95/mac"
                )
            )
        else:
            command = f'powershell.exe -ExecutionPolicy Bypass -File "{core.ROOT / "scripts/run.ps1"}" --capture'
            self.info(
                "Atajo global de Windows",
                "Creá un acceso directo para micrófono con este comando. La captura de audio del equipo aún no está disponible en Windows.\n\n"
                + command
                + "\n\nPara abrir la aplicación y el historial: "
                + f'powershell.exe -ExecutionPolicy Bypass -File "{core.ROOT / "scripts/run.ps1"}" --open-history',
            )

    def _build_review(self):
        box = self._page(
            "Revisar ticket", "Corregí el contenido antes de confirmar su envío a Jira."
        )
        self.review_info = label("", "muted")
        box.addWidget(self.review_info)
        box.addWidget(label("TÍTULO", "eyebrow"))
        self.review_title = QLineEdit()
        box.addWidget(self.review_title)
        row = QHBoxLayout()
        row.addWidget(label("Tipo"))
        self.review_type = ClickComboBox()
        self.review_type.addItems(["Tarea", "Error", "Historia"])
        self.review_type.currentTextChanged.connect(self.review_type_changed)
        row.addWidget(self.review_type)
        row.addWidget(button("Consultar tipos", self.load_jira_types))
        row.addWidget(label("Épica"))
        self.review_epic = ClickComboBox()
        row.addWidget(self.review_epic, 1)
        box.addLayout(row)
        box.addWidget(label("DESCRIPCIÓN", "eyebrow"))
        self.review_body = QTextEdit()
        box.addWidget(self.review_body, 1)
        row = QHBoxLayout()
        row.addWidget(button("Campos de Jira…", self.show_review_fields))
        self.review_fields_status = label("", "muted")
        row.addWidget(self.review_fields_status, 1)
        box.addLayout(row)

        class FieldsDialog(QDialog):
            def flush(self):
                return True

            def accept(self):
                if self.flush():
                    super().accept()

            def reject(self):
                if self.flush():
                    super().reject()

            def closeEvent(self, event):
                if self.flush():
                    super().closeEvent(event)
                else:
                    event.ignore()

        self.review_fields_dialog = FieldsDialog(self)
        self.review_fields_dialog.setObjectName("instructionsDialog")
        self.review_fields_dialog.setWindowTitle("Campos de Jira")
        self.review_fields_dialog.resize(760, 600)
        fields_box = QVBoxLayout(self.review_fields_dialog)
        fields_box.addWidget(label("Campos de Jira", "sectionTitle"))
        fields_box.addWidget(
            label("Revisá los valores antes de enviar. * indica un campo obligatorio.", "muted")
        )
        self.review_fields_description = label("", "muted")
        fields_box.addWidget(self.review_fields_description)
        self.review_fields_error = label("", "muted")
        self.review_fields_error.hide()
        fields_box.addWidget(self.review_fields_error)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.review_fields_content = QWidget()
        self.review_fields_layout = QVBoxLayout(self.review_fields_content)
        scroll.setWidget(self.review_fields_content)
        fields_box.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        self.review_fields_close_button = button(
            "Guardar y cerrar", self.review_fields_dialog.accept, "primary"
        )
        footer.addWidget(self.review_fields_close_button)
        footer.addStretch()
        fields_box.addLayout(footer)
        self.review_field_editors = {}
        self.review_fields_type = None
        self.review_fields_dialog.flush = self.flush_review_fields
        row = QHBoxLayout()
        row.addWidget(button("Volver", lambda: self.show_page(1)))
        row.addWidget(button("Guardar cambios", self.save_review))
        row.addWidget(button("Ver salida", self.show_agent_response))
        self.delete_review = button("Borrar borrador", self.delete_review_ticket, "danger")
        row.addWidget(self.delete_review)
        row.addStretch()
        self.send = button("Enviar a Jira", self.publish, "primary")
        row.addWidget(self.send)
        box.addLayout(row)

    def _build_skill(self):
        box = self._page(
            "Instrucciones para tickets",
            "Definí el formato y las reglas para Cursor u Ollama. El envío siempre requiere confirmación.",
        )
        self.skill_location = label(f"Archivo personal: {core.custom_skill_path()}", "muted")
        box.addWidget(self.skill_location)
        self.skill = QTextEdit()
        box.addWidget(self.skill, 1)
        row = QHBoxLayout()
        row.addWidget(button("Volver", lambda: self.show_page(3)))
        row.addWidget(button("Restaurar plantilla", self.restore_skill))
        row.addStretch()
        row.addWidget(label("Los cambios se guardan automáticamente.", "muted"))
        box.addLayout(row)

    def show_page(self, page):
        current = self.pages.currentIndex()
        if self.busy and page != current:
            self.status.setText("Esperá a que termine la operación actual.")
            return False
        if self.recording and not self.quick_recording and page != 0:
            self.info("Grabación en curso", "Detené la grabación antes de cambiar de pantalla.")
            return False
        if current == 3 and page != 3 and self.settings_dirty and not self.save_settings():
            return False
        if current == 4 and page != 4 and self.review_id is not None and not self.save_review():
            return False
        if current == 5 and page != 5 and not self.confirm_skill_changes():
            return False
        self.pages.setCurrentIndex(page)
        selected = {4: 1, 5: 3}.get(page, page)
        for number, b in self.nav.items():
            b.setChecked(number == selected)
        return True

    def toggle_maximize(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def sync_project_fields(self):
        if self.busy or (self.settings_dirty and not self.save_settings()):
            return
        site = self.settings["jira_site"].text().strip()
        projects = self.config.get("projects", [])
        if not site or not projects:
            self.error("Falta configuración", "Indicá el sitio y al menos un proyecto Jira.")
            return

        def done(errors):
            if errors:
                self.jira_sync_status.setText("Sincronización parcial. Revisá los detalles.")
                self.error(
                    "No se pudieron sincronizar todos los campos",
                    "\n".join(f"{name}: {message}" for name, message in errors.items()),
                )
            else:
                self.jira_sync_status.setText(core.jira_cache_status(site, projects))
            self.status.setText(self.jira_sync_status.text())

        self.background(
            lambda: core.sync_jira_fields(site, projects), done, "Sincronizando campos de Jira…"
        )

    def dialog(self, title, message, icon=QMessageBox.Icon.NoIcon):
        dialog = QMessageBox(self)
        dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setWindowTitle(title)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(title)
        dialog.setInformativeText(str(message))
        dialog.setIcon(icon)
        dialog.setMinimumWidth(430)
        dialog.setStyleSheet(
            "QMessageBox { background: #202a35; border: 1px solid #526477; border-radius: 8px; }"
        )
        return dialog

    def show_notice(self, dialog, *, autoclose=True):
        if not self.isVisible():
            self.show_main()
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setParent(self, Qt.WindowType.Widget)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.notice_windows.add(dialog)
        dialog.finished.connect(lambda _: self.notice_windows.discard(dialog))
        if autoclose:
            timer = QTimer(dialog)
            timer.setObjectName("noticeTimeout")
            timer.setSingleShot(True)
            timer.timeout.connect(dialog.close)
            text_length = sum(len(item.text()) for item in dialog.findChildren(QLabel))
            timer.start(max(15_000, min(45_000, 5_000 + text_length * 75)))
        dialog.adjustSize()
        self.position_notice(dialog)
        dialog.show()
        dialog.raise_()
        return dialog

    def position_notice(self, dialog):
        dialog.move(
            max(0, (self.width() - dialog.width()) // 2),
            max(0, (self.height() - dialog.height()) // 2),
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for dialog in getattr(self, "notice_windows", ()):
            self.position_notice(dialog)

    def info(self, title, message):
        dialog, actions = self.notice_dialog(title, message)
        actions.addStretch()
        actions.addWidget(button("Aceptar", dialog.accept, "primary"))
        return self.show_notice(dialog)

    def error(self, title, message):
        dialog, actions = self.notice_dialog(title, message, error=True)
        actions.addStretch()
        actions.addWidget(button("Aceptar", dialog.accept, "primary"))
        return self.show_notice(dialog)

    def notice_dialog(self, title, message, error=False):
        dialog = QDialog(self)
        dialog.setObjectName("noticePanel")
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(430)
        border = "#d37777" if error else "#526477"
        dialog.setStyleSheet(
            f"QDialog#noticePanel {{ background: #202a35; border: 1px solid {border}; border-radius: 8px; }}"
        )
        box = QVBoxLayout(dialog)
        box.setContentsMargins(22, 18, 22, 18)
        box.setSpacing(12)
        box.addWidget(label(title, "sectionTitle"))
        box.addWidget(label(str(message)))
        actions = QHBoxLayout()
        box.addLayout(actions)
        return dialog, actions

    def confirm(self, title, message):
        dialog = self.dialog(title, message, QMessageBox.Icon.Question)
        no = dialog.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        yes = dialog.addButton("Sí, continuar", QMessageBox.ButtonRole.AcceptRole)
        dialog.setDefaultButton(no)
        dialog.exec()
        return dialog.clickedButton() == yes

    def show_media_failure(self, title, message, details):
        if QApplication.activeModalWidget() is not None:
            self.deferred_failures.append((title, message, details))
            return None
        if not self.isVisible():
            self.show_main()
        diagnostic = self.media_diagnostics(title, details)
        dialog = QDialog(self, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setObjectName("mediaError")
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(500)
        dialog.setStyleSheet(
            "QDialog#mediaError { background: #202a35; border: 1px solid #526477; border-radius: 8px; }"
        )
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(label(title, "sectionTitle"))
        layout.addWidget(label(message))
        detail_view = QTextEdit()
        detail_view.setReadOnly(True)
        detail_view.setPlainText(diagnostic)
        detail_view.setMinimumHeight(140)
        detail_view.hide()
        layout.addWidget(detail_view)
        row = QHBoxLayout()
        toggle = button("Ver detalles", lambda: None)

        def toggle_details():
            visible = not detail_view.isVisible()
            detail_view.setVisible(visible)
            toggle.setText("Ocultar detalles" if visible else "Ver detalles")
            dialog.adjustSize()
            self.position_notice(dialog)
            timer = dialog.findChild(QTimer, "noticeTimeout")
            if timer:
                timer.start(45_000)

        toggle.clicked.connect(toggle_details)
        row.addWidget(toggle)

        def copy_details():
            QApplication.clipboard().setText(diagnostic)
            self.status.setText("Detalles copiados al portapapeles.")

        copy = button("Copiar detalles", copy_details)
        close = button("Cerrar", dialog.accept, "primary")
        for action, texts in (
            (toggle, ("Ver detalles", "Ocultar detalles")),
            (copy, ("Copiar detalles",)),
            (close, ("Cerrar",)),
        ):
            action.ensurePolished()
            metrics = action.fontMetrics()
            action.setMinimumSize(
                max(metrics.horizontalAdvance(text) for text in texts) + 32, metrics.height() + 24
            )
        row.addWidget(copy)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        report = label('<a href="report">Reportar un error en GitHub</a>', "muted")
        report.linkActivated.connect(lambda _: self.open_issue_tracker())
        layout.addWidget(report)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.failure_windows.add(dialog)
        dialog.finished.connect(lambda _: self.failure_windows.discard(dialog))
        self.show_notice(dialog)
        dialog.raise_()
        return dialog

    def media_diagnostics(self, title, details):
        system = f"{platform.system()} {platform.release()} ({platform.machine()})"
        if sys.platform.startswith("linux"):
            try:
                distro = platform.freedesktop_os_release()
                system += f"\nDistribución: {distro.get('PRETTY_NAME', distro.get('NAME', 'desconocida'))}"
            except OSError:
                pass
        elif sys.platform == "darwin":
            system += f"\nmacOS: {platform.mac_ver()[0] or 'desconocido'}"
        elif sys.platform == "win32":
            system += f"\nWindows: {platform.version()}"
        packages = (
            "pip",
            "numpy",
            "sounddevice",
            "faster-whisper",
            "ctranslate2",
            "huggingface-hub",
            "tokenizers",
            "onnxruntime",
            "av",
            "PySide6-Essentials",
            "shiboken6",
            "psutil",
        )
        versions = []
        for package in packages:
            try:
                versions.append(f"{package}: {importlib.metadata.version(package)}")
            except importlib.metadata.PackageNotFoundError:
                versions.append(f"{package}: no instalado")
            except Exception:
                versions.append(f"{package}: versión no disponible")
        try:
            import av

            libraries = ", ".join(
                f"{name} {'.'.join(map(str, version))}"
                for name, version in sorted(av.library_versions.items())
            )
        except Exception:
            libraries = "no disponible"
        try:
            import sounddevice

            portaudio = sounddevice.get_portaudio_version()[1]
        except Exception:
            portaudio = "no disponible"
        audio_info = "no disponible"
        audio_source = self.recording_source or self.source.currentData()
        match = re.search(r"(?:^|\n)Nota: #(\d+)(?:\n|$)", str(details))
        if match:
            try:
                capture = self.get_capture(int(match.group(1)))
                if capture and capture["audio_source"]:
                    audio_source = capture["audio_source"]
                path = Path(capture["audio_file"]) if capture and capture["audio_file"] else None
                if path and path.is_file():
                    with wave.open(str(path), "rb") as audio:
                        rate = audio.getframerate()
                        duration = audio.getnframes() / rate if rate else 0
                        audio_info = (
                            f"{duration:.1f} s, {rate} Hz, {audio.getnchannels()} canal(es), "
                            f"{audio.getsampwidth() * 8} bits, {path.stat().st_size} bytes"
                        )
            except Exception:
                pass
        return (
            f"BacklogTalk: {VERSION}\nSistema: {system}\n"
            f"Python: {platform.python_implementation()} {platform.python_version()}\n"
            f"Qt: {qVersion()}\nPortAudio: {portaudio}\nBibliotecas FFmpeg: {libraries}\n"
            f"Audio guardado: {audio_info}\n"
            f"Modelo de transcripción (configuración actual): {self.whisper_model.currentData()}\n"
            f"Idioma de transcripción: {self.transcription_language.currentData()}\n"
            f"Fuente de audio: {audio_source}\n"
            f"Operación: {title}\n\nDependencias:\n"
            + "\n".join(versions)
            + f"\n\nDetalles:\n{details}"
        )

    def update_error_details_button(self):
        ids = self.selected_ids(self.history)
        try:
            error = core.get_capture_error(ids[0]) if len(ids) == 1 else None
        except Exception:
            error = None
        self.error_details_button.setVisible(error is not None)

    def show_selected_error(self):
        ids = self.selected_ids(self.history)
        if len(ids) != 1:
            return
        error = core.get_capture_error(ids[0])
        if error:
            self.show_media_failure("Problema de audio", error["message"], error["details"])

    def report_capture_problem(self, capture_id, kind, message, details):
        diagnostic = f"Nota: #{capture_id}\nTipo: {kind}\n{details}"
        try:
            core.save_capture_error(capture_id, kind, message, diagnostic)
        except Exception as error:
            diagnostic += f"\nNo se pudo guardar este diagnóstico: {error}"
        self.show_media_failure(
            "No se pudo procesar el audio" if kind != "sin_voz" else "No se detectó voz",
            message,
            diagnostic,
        )

    def manage_epics(self):
        if self.settings_dirty and not self.save_settings():
            return
        project = self.project.currentText().strip().upper()
        site = self.settings["jira_site"].text().strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
            self.error("Proyecto inválido", "Elegí un proyecto de Jira para gestionar sus épicas.")
            return
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", site):
            self.error("Sitio de Jira", "Configurá el dominio de Jira para gestionar sus épicas.")
            return
        scope = core.epic_favorites(self.config, site)
        dialog = EpicManager(
            self,
            project,
            site,
            scope.get("projects", {}).get(project, []),
            scope.get("names", {}),
            lambda selected, names: self.save_epic_favorites(site, project, selected, names),
        )
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def save_epic_favorites(self, site, project, selected, names):
        config = dict(self.config)
        scope = core.epic_favorites(config, site)
        sites = dict(config.get("favorite_epics_by_site", {}))
        favorites = dict(scope.get("projects", {}))
        favorites[project] = selected
        sites[site.strip().lower()] = {"projects": favorites, "names": names}
        config["favorite_epics_by_site"] = sites
        core.save_config(config)
        current = self.epic_key()
        self.config = config
        if current and current not in selected:
            self.epic.setCurrentText("Sin épica")
        elif current:
            name = names.get(current)
            self.epic.setCurrentText(f"{name} / {current}" if name else current)
        self.fill_epics()
        self.status.setText("Épicas favoritas guardadas en este equipo.")

    def project_changed(self, *_):
        self.epic.setCurrentText("Sin épica")
        self.fill_epics()

    def fill_epics(self, *_):
        if not hasattr(self, "epic"):
            return
        project = self.project.currentText().strip().upper()
        current = self.epic.currentText()
        scope = core.epic_favorites(self.config, self.config.get("jira_site", ""))
        names = scope.get("names", {})
        self.epic.blockSignals(True)
        self.epic.clear()
        self.epic.addItem("Sin épica")
        for key in scope.get("projects", {}).get(project, []):
            self.epic.addItem(f"{names[key]} / {key}" if key in names else key)
        self.epic.setCurrentText(current if current else "Sin épica")
        self.epic.blockSignals(False)

    def epic_key(self):
        value = self.epic.currentText().strip().upper().rsplit(" / ", 1)[-1]
        return "" if value == "SIN ÉPICA" else value

    def schedule_settings_save(self, *_):
        self.settings_dirty = True
        self.settings_timer.start()
        self.status.setText("Guardando configuración…")

    def settings_error(self, title, message, notify):
        self.status.setText(f"Configuración sin guardar: {message}")
        if notify:
            self.error(title, message)
        return False

    def save_settings(self, notify=True):
        self.settings_timer.stop()
        projects = [
            x.strip().upper() for x in self.settings["projects"].text().split(",") if x.strip()
        ]
        if not projects:
            return self.settings_error(
                "Falta proyecto", "Configurá al menos un proyecto Jira.", notify
            )
        sequence = QKeySequence.fromString(
            self.shortcut_edit.text().strip(), QKeySequence.SequenceFormat.PortableText
        )
        shortcut = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        modifier_keys = (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
            Qt.Key.Key_AltGr,
            Qt.Key.Key_unknown,
        )
        if not shortcut or sequence.count() != 1 or sequence[0].key() in modifier_keys:
            return self.settings_error(
                "Atajo inválido", "Escribí una combinación válida, por ejemplo Ctrl+Alt+J.", notify
            )
        system_sequence = QKeySequence.fromString(
            self.shortcut_system_edit.text().strip(), QKeySequence.SequenceFormat.PortableText
        )
        shortcut_system = system_sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if (
            not shortcut_system
            or system_sequence.count() != 1
            or system_sequence[0].key() in modifier_keys
            or shortcut_system == shortcut
        ):
            return self.settings_error(
                "Atajo inválido", "Indicá otro atajo válido para grabar la reunión.", notify
            )
        open_sequence = QKeySequence.fromString(
            self.shortcut_open_edit.text().strip(), QKeySequence.SequenceFormat.PortableText
        )
        shortcut_open = open_sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if (
            not shortcut_open
            or open_sequence.count() != 1
            or open_sequence[0].key() in modifier_keys
            or shortcut_open in (shortcut, shortcut_system)
        ):
            return self.settings_error(
                "Atajo inválido", "Indicá otro atajo válido para abrir el historial.", notify
            )
        if self.transcription_language.currentData() == "es" and str(
            self.whisper_model.currentData()
        ).endswith(".en"):
            return self.settings_error(
                "Modelo incompatible",
                "Para español, elegí un modelo multilingüe de Whisper.",
                notify,
            )
        cursor_model = self.cursor_model.currentText().strip()
        if cursor_model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cursor_model):
            return self.settings_error(
                "Modelo de Cursor inválido", "Elegí un ID de la lista de Cursor.", notify
            )
        old_projects = self.config.get("projects", [])
        old_site = self.config.get("jira_site", "").strip().lower()
        updated = dict(self.config)
        updated.update(
            projects=projects,
            model_backend=self.backend.currentText(),
            audio_device=self.mics.currentText(),
            cursor_model=cursor_model,
            shortcut=shortcut,
            shortcut_system=shortcut_system,
            shortcut_open=shortcut_open,
            whisper_model=self.whisper_model.currentData(),
            transcription_language=self.transcription_language.currentData(),
            last_project=self.project.currentText().strip().upper(),
            **{key: self.settings[key].text().strip() for key in ("jira_site", "ollama_model")},
        )
        core.scope_legacy_epic_favorites(updated)
        try:
            core.save_config(updated)
        except OSError as error:
            return self.settings_error("No se pudo guardar", str(error), notify)
        self.config = updated
        self.settings_dirty = False
        self.jira_sync_status.setText(core.jira_cache_status(updated["jira_site"], projects))
        if updated.get("jira_site", "").strip().lower() != old_site:
            self.epic.setCurrentText("Sin épica")
            self.fill_epics()
        if projects != old_projects:
            current = self.project.currentText()
            epic = self.epic.currentText()
            self.project.blockSignals(True)
            self.project.clear()
            self.project.addItems(projects)
            self.project.setCurrentText(current)
            self.project.blockSignals(False)
            self.fill_epics()
            self.epic.setCurrentText(epic)
        self.shortcut.setKey(QKeySequence(shortcut))
        self.system_shortcut.setKey(QKeySequence(shortcut_system))
        self.open_shortcut.setKey(QKeySequence(shortcut_open))
        self.refresh_shortcut_summary()
        self.status.setText("Configuración guardada automáticamente.")
        return True

    def schedule_skill_save(self):
        if self.pages.currentIndex() != 5:
            return
        self.skill_timer.start()
        self.status.setText("Guardando instrucciones…")

    def save_note(self):
        text = self.note.toPlainText().strip()
        project = self.project.currentText().strip().upper()
        if not text:
            self.info("Nota vacía", "Grabá o escribí una nota primero.")
            return None
        if not project:
            self.info("Falta proyecto", "Elegí un proyecto Jira.")
            return None
        try:
            if self.capture_id is None:
                self.capture_id = core.add_capture(
                    project, self.epic_key() or None, transcript=text
                )
            else:
                core.update_capture(self.capture_id, text, project, self.epic_key() or None)
            self.config["last_project"] = project
            core.save_config(self.config)
            self.refresh()
            self.status.setText(f"Nota #{self.capture_id} guardada en Historial.")
            return self.capture_id
        except Exception as error:
            self.error("No se pudo guardar", error)
            return None

    def new_capture(self):
        if self.busy or self.recording:
            self.info(
                "Operación en curso", "Esperá a que termine la operación o detengas la grabación."
            )
            return
        if self.note.toPlainText().strip() and self.save_note() is None:
            return
        self.capture_id = None
        self.note.clear()
        self.epic.setCurrentIndex(0)
        self.show_page(0)
        self.status.setText("Nueva nota lista; elegí una épica si la necesitás.")

    def refresh_shortcut_summary(self):
        if self.shortcut_lookup_running:
            return
        self.shortcut_lookup_running = True

        def lookup():
            try:
                bindings = shortcuts.gnome_global_shortcuts(core.ROOT)
            except Exception:
                bindings = None
            self.results.put(("global_shortcuts", None, bindings))

        threading.Thread(target=lookup, name="backlogtalk-shortcuts", daemon=True).start()

    def display_shortcut_summary(self, bindings):
        if bindings is None:
            mic = self.shortcut.key().toString(QKeySequence.SequenceFormat.NativeText)
            meeting = self.system_shortcut.key().toString(QKeySequence.SequenceFormat.NativeText)
            opening = self.open_shortcut.key().toString(QKeySequence.SequenceFormat.NativeText)
            self.shortcut_value.setText(
                f"Mic (app)  {mic}\nReunión (app)  {meeting}\nAbrir (app)  {opening}"
            )
            self.shortcut_value.setToolTip(
                "Estos son los atajos de la aplicación. Los atajos globales se configuran en el sistema y no se pueden consultar aquí."
            )
            return

        def display(action):
            return " / ".join(bindings[action]) or "Sin asignar"

        self.shortcut_value.setText(
            f"Mic  {display('mic')}\nReunión  {display('meeting')}\nAbrir  {display('open')}"
        )
        self.shortcut_value.setToolTip("Atajos globales configurados en el sistema.")

    def show_main(self):
        self.refresh_shortcut_summary()
        self.capture_only = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def shortcut_open_requested(self):
        if self.shortcut_editor_focused():
            self.shortcut_help.setText(
                "El sistema usó ese atajo. Para configurarlo, elegí «Escribir»."
            )
            return
        self.open_with_history()

    def open_with_history(self):
        self.show_main()
        if self.quick_id is not None:
            self.show_capture_in_history(self.quick_id)
        else:
            self.show_page(2)

    def show_capture_in_history(self, capture_id):
        self.refresh()
        if not self.show_page(2):
            return False
        for row in range(self.history.count()):
            item = self.history.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == capture_id:
                self.history.clearSelection()
                self.history.setCurrentItem(item)
                item.setSelected(True)
                self.history.scrollToItem(item)
                self.status.setText(f"Nota #{capture_id} seleccionada en Historial.")
                return True
        return False

    def source_changed(self):
        if self.recording:
            return
        source = self.source.currentData()
        self.record_button.setText("Elegir archivo…" if source == "file" else "●  Grabar nota")
        hints = {
            "mic": "Se guarda y transcribe automáticamente.",
            "system": "Graba tu micrófono y el audio del equipo."
            if sys.platform.startswith("linux")
            else "Audio del equipo aún no disponible en este sistema.",
            "file": "Importá audio o video; se extraerá el audio.",
        }
        self.record_hint.setText(hints[source])
        self.source_animation.stop()
        self.source_animation.setStartValue(0.3)
        self.source_animation.setEndValue(1.0)
        self.source_animation.start()

    def shortcut_capture(self, source="mic"):
        if self.migrating or self.record_confirmation_pending:
            return
        if self.recording:
            if self.recording_source == source:
                self.stop_record()
            else:
                message = "Ya hay una grabación activa. Detenela antes de cambiar la fuente."
                self.status.setText(message)
                if self.popup.isVisible():
                    self.popup.message.setText(message)
            return
        if self.shortcut_editor_focused():
            self.shortcut_help.setText(
                "El sistema usó ese atajo. Para configurarlo, elegí «Escribir»."
            )
            return
        self.start_record(quick=True, source=source)

    def toggle_record(self):
        if self.recording:
            self.stop_record()
        elif self.source.currentData() == "file":
            self.choose_media()
        else:
            self.start_record()

    def start_record(self, quick=False, source=None):
        if self.record_confirmation_pending or self.recording or (self.busy and not quick):
            return
        source = source or self.source.currentData()
        if source == "file":
            self.choose_media()
            return
        if not quick and self.note.toPlainText().strip():
            self.info("Nota en edición", "Elegí «Guardar y crear otra» antes de grabar.")
            return
        project = self.project.currentText().strip().upper()
        if not project:
            self.show_main()
            self.info("Falta proyecto", "Elegí un proyecto Jira.")
            return
        try:
            if source == "system":
                output = system_output_device()
                self.recorder = MeetingRecorder()
                self.recorder.start(self.mics.currentData(), output)
            else:
                self.recorder = (
                    VoiceRecorder() if isinstance(self.recorder, MeetingRecorder) else self.recorder
                )
                self.recorder.start(self.mics.currentData())
        except Exception:
            self.show_media_failure(
                "No se pudo iniciar la grabación",
                "Revisá que las fuentes de audio estén disponibles y volvé a intentarlo.",
                traceback.format_exc(),
            )
            return
        self.quick_recording = quick
        if quick:
            self.quick_id = None
        self.recording_source = source
        self.audio_write_error_reported = False
        self.quick_context = (project, self.epic_key() or None)
        self.recording = True
        self.source.setEnabled(False)
        self.meter.show()
        self.record_button.setText("■  Detener")
        self.cancel_record_button.show()
        self.cancel_record_button.setEnabled(True)
        try:
            if source == "system" and hasattr(self.recorder, "microphone"):
                paths = [self.recorder.microphone.path, self.recorder.system.path]
            else:
                paths = [getattr(self.recorder, "path", None)]
            self.record_operation = core.begin_operation(
                "grabación", details="Pistas parciales: " + ", ".join(str(p) for p in paths if p)
            )
        except Exception:
            self.record_operation = None
            self.show_media_failure(
                "No se pudo activar la recuperación",
                "La grabación sigue activa, pero no se pudo guardar el estado de recuperación.",
                traceback.format_exc(),
            )
        self.record_hint.setText(
            "Grabando micrófono y equipo…" if source == "system" else "Grabando micrófono…"
        )
        self.status.setText(
            "Grabando micrófono y audio del equipo."
            if source == "system"
            else "Grabando micrófono."
        )
        if quick:
            self.popup.recording_started()
        else:
            self.show_page(0)
        self.set_tray_recording(True)

    def choose_media(self):
        if self.busy or self.recording:
            return
        if self.pending or self.manual_transcribing:
            self.info(
                "Transcripción en curso",
                "Esperá a que termine o cancelá la transcripción antes de importar otro archivo.",
            )
            return
        project = self.project.currentText().strip().upper()
        epic = self.epic_key() or None
        if not project:
            self.info("Falta proyecto", "Elegí un proyecto Jira.")
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Elegí audio o video",
            str(Path.home()),
            "Audio y video (*.wav *.mp3 *.m4a *.aac *.ogg *.opus *.flac *.mp4 *.mkv *.mov *.webm *.avi);;Todos los archivos (*)",
        )
        if not filename:
            return
        try:
            operation = core.begin_operation("importación", details=f"Archivo original: {filename}")
        except Exception:
            self.show_media_failure(
                "No se pudo importar",
                "No fue posible iniciar la importación. El archivo original no se modificó.",
                traceback.format_exc(),
            )
            return
        self.importing = True
        self.import_progress = None
        self.update_progress()

        def work():
            path = import_media(
                Path(filename),
                progress=lambda pos, total, elapsed: self.results.put(
                    ("import_progress", operation, (pos, total, elapsed))
                ),
                operation_id=operation,
            )
            try:
                capture_id = core.add_capture(project, epic, audio=str(path), source="file")
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return capture_id, path

        def done(result):
            capture_id, path = result
            self.queue_transcription(capture_id, path)
            core.finish_operation(operation)
            self.refresh()
            self.status.setText(f"Audio #{capture_id} importado y guardado; transcribiendo…")

        self.background(
            work,
            done,
            "Extrayendo y guardando audio…",
            context={"kind": "importación", "operation": operation, "path": filename},
        )

    def stop_record(self):
        if self.record_confirmation_pending or not self.recording or self.recording_stopping:
            return
        quick = self.quick_recording
        source = self.recording_source
        self.recording_stopping = True
        self.record_button.setEnabled(False)
        self.cancel_record_button.setEnabled(False)
        self.popup.action.setEnabled(False)
        self.popup.cancel.setEnabled(False)
        self.status.setText("Guardando grabación…")

        def stop():
            try:
                path = (
                    self.recorder.stop_sources()
                    if source == "system"
                    else self.recorder.stop_to_file()
                )
                self.results.put(("record_stopped", (quick, source), {"path": path}))
            except Exception as error:
                self.results.put(
                    (
                        "record_stopped",
                        (quick, source),
                        {"error": str(error), "details": traceback.format_exc()},
                    )
                )

        threading.Thread(target=stop, name="backlogtalk-stop-recording", daemon=True).start()

    def cancel_record(self):
        if self.record_confirmation_pending or not self.recording or self.recording_stopping:
            return
        self.record_confirmation_pending = True
        try:
            confirmed = self.confirm(
                "Cancelar grabación", "¿Descartar esta grabación? No se guardará una nota."
            )
        finally:
            self.record_confirmation_pending = False
        if not confirmed:
            return
        if not self.recording or self.recording_stopping:
            return
        context = (self.quick_recording, self.recording_source)
        self.recording_stopping = True
        self.record_button.setEnabled(False)
        self.cancel_record_button.setEnabled(False)
        self.popup.action.setEnabled(False)
        self.popup.cancel.setEnabled(False)
        self.status.setText("Cancelando grabación…")

        def discard():
            try:
                self.recorder.discard()
                self.results.put(("record_stopped", context, {"cancelled": True}))
            except Exception as error:
                self.results.put(
                    (
                        "record_stopped",
                        context,
                        {
                            "error": str(error),
                            "details": traceback.format_exc(),
                            "cancelling": True,
                        },
                    )
                )

        threading.Thread(target=discard, name="backlogtalk-cancel-recording", daemon=True).start()

    def finish_stop_record(self, context, result):
        quick, source = context
        self.recording_stopping = False
        self.record_button.setEnabled(not self.busy)
        self.cancel_record_button.setEnabled(True)
        self.popup.action.setEnabled(True)
        self.popup.cancel.setEnabled(True)
        if result.get("cancelled"):
            self.recording_stopped()
            self.popup.hide()
            try:
                core.finish_operation(self.record_operation)
                self.record_operation = None
            except Exception as error:
                self.show_media_failure(
                    "No se pudo terminar la cancelación",
                    "La grabación fue descartada, pero no se pudo actualizar su estado.",
                    str(error),
                )
                return
            self.status.setText("Grabación cancelada.")
            if self.capture_only and not self.tray_available():
                self.request_exit()
            return
        if "error" in result:
            self.close_after_recording = False
            still_recording = self.recorder_is_active()
            if not still_recording:
                self.recording_stopped()
            self.show_main()
            if not still_recording:
                self.popup.hide()
            details = result["details"]
            if not still_recording:
                recorders = (
                    (self.recorder.microphone, self.recorder.system)
                    if source == "system" and hasattr(self.recorder, "microphone")
                    else (self.recorder,)
                )
                writer_open = any(
                    getattr(recorder, "writer", None) is not None for recorder in recorders
                )
                if not writer_open and not result.get("cancelling"):
                    try:
                        core.finish_operation(self.record_operation)
                        self.record_operation = None
                    except Exception:
                        details += "\n" + traceback.format_exc()
            cancelling = result.get("cancelling", False)
            self.show_media_failure(
                "No se pudo cancelar la grabación"
                if cancelling
                else "No se pudo guardar la grabación",
                "Revisá la fuente de audio. Si se conservó una pista parcial, su ubicación aparece en los detalles.",
                details,
            )
            return
        path = result["path"]
        recorders = (
            (self.recorder.microphone, self.recorder.system)
            if source == "system" and hasattr(self.recorder, "microphone")
            else (self.recorder,)
        )
        overflowed = any(getattr(recorder, "overflowed", False) for recorder in recorders)
        self.recording_stopped()
        try:
            project, epic = self.quick_context
            job = create_mix_job(path, project, epic) if source == "system" else None
            audio_path = job.output if job else path
            capture_id = core.add_capture(
                project, epic, audio=str(audio_path), source="meeting" if job else source
            )
            path = job or path
            if not quick:
                self.capture_id = capture_id
            if quick or self.popup.isVisible():
                self.quick_id = capture_id
            self.queue_transcription(capture_id, path, 0)
            core.finish_operation(self.record_operation)
            self.record_operation = None
            self.refresh()
            self.status.setText(f"Audio #{capture_id} guardado; transcribiendo…")
            if quick or self.popup.isVisible():
                self.popup.saved(capture_id)
            if overflowed:
                message = "El audio se guardó, pero puede tener cortes porque la entrada no pudo procesarse a tiempo."
                core.save_capture_error(
                    capture_id,
                    "audio_overflow",
                    message,
                    "PortAudio informó pérdida de muestras durante la grabación.",
                )
                self.show_media_failure(
                    "La grabación puede tener cortes",
                    message,
                    "PortAudio informó pérdida de muestras durante la grabación.",
                )
                self.refresh()
        except Exception as error:
            self.close_after_recording = False
            self.show_main()
            self.popup.hide()
            detail = (
                f"{error}\nPistas conservadas: {path[0]} y {path[1]}"
                if source == "system" and isinstance(path, tuple)
                else str(error)
            )
            self.show_media_failure(
                "No se pudo guardar la grabación",
                "El audio puede estar conservado como archivo parcial; revisá los detalles.",
                detail + "\n" + traceback.format_exc(),
            )

    def recorder_is_active(self):
        if self.recording_source == "system" and hasattr(self.recorder, "active"):
            return self.recorder.active
        stream = getattr(self.recorder, "stream", None)
        try:
            return stream is not None and stream.active
        except Exception:
            return stream is not None

    def recording_stopped(self):
        self.recording = False
        self.quick_recording = False
        self.recording_source = None
        self.meter.hide()
        self.cancel_record_button.hide()
        self.source.setEnabled(not self.busy)
        self.waveform.set_level(0, False)
        self.source_changed()
        self.set_tray_recording(False)

    def queue_transcription(self, capture_id, path, priority=0):
        if capture_id not in self.pending:
            self.asr_errors.pop(capture_id, None)
            self.pending.add(capture_id)
            cancel = threading.Event()
            self.cancel_events[capture_id] = cancel
            self.jobs.put(
                (
                    priority,
                    next(self.order),
                    capture_id,
                    path,
                    self.whisper_model.currentData(),
                    self.transcription_language.currentData(),
                    cancel,
                )
            )
            self.update_progress()

    def cancel_transcription(self):
        capture_id = self.running_id if self.running_id is not None else self.manual_capture_id
        cancel = (
            self.cancel_events.get(capture_id)
            if self.running_id is not None
            else self.manual_cancel
        )
        if capture_id is None or cancel is None or cancel.is_set():
            return
        try:
            core.cancel_capture_transcription(capture_id)
        except Exception as error:
            self.error("No se pudo cancelar", error)
            return
        cancel.set()
        self.status.setText("Cancelando transcripción… el audio se conserva.")
        self.update_progress()
        self.refresh()

    def resume_pending(self):
        if self.migrating:
            QTimer.singleShot(500, self.resume_pending)
            return
        if not self.recovery_checked:
            self.recovery_checked = True
            try:
                core.recover_deleted_audio()
                for operation in core.interrupted_operations():
                    kind = operation["kind"]
                    details = f"Operación interrumpida: {kind}\nInicio: {operation['started_at']}\nPID anterior: {operation['owner_pid']}\n{operation['details']}\nLa causa del cierre no está disponible."
                    if kind == "importación":
                        cleanup_interrupted_import(operation["operation_id"])
                    if operation["capture_id"] is not None and self.get_capture(
                        operation["capture_id"]
                    ):
                        capture = self.get_capture(operation["capture_id"])
                        audio = capture["audio_file"]
                        available = bool(
                            audio and (Path(audio).is_file() or self.mix_job_for_output(audio))
                        )
                        message = (
                            "La aplicación se cerró antes de terminar. El audio se conserva y podés reintentarlo desde Historial."
                            if available
                            else "La aplicación se cerró antes de terminar y no se encontró el audio. Revisá los detalles."
                        )
                        self.report_capture_problem(
                            operation["capture_id"], "interrumpida", message, details
                        )
                    else:
                        message = "La aplicación se cerró mientras procesaba audio. Revisá los detalles; el archivo original no se modificó."
                        self.show_media_failure("Proceso interrumpido", message, details)
                    core.finish_operation(operation["operation_id"])
            except Exception:
                self.show_media_failure(
                    "No se pudo revisar la recuperación",
                    "No fue posible comprobar un trabajo anterior.",
                    traceback.format_exc(),
                )
        discarded = set(core.discarded_mix_manifests())
        for manifest in discarded:
            try:
                core.finish_discarded_mix_files(manifest)
            except Exception as error:
                self.status.setText(f"No se pudo completar un borrado de audio: {str(error)[:120]}")
        for manifest in core.AUDIO_DIR.glob("mix-*.json"):
            if manifest in discarded:
                continue
            try:
                job = read_mix_job(manifest)
                with core.db_connect() as db:
                    row = db.execute(
                        "SELECT id FROM captures WHERE audio_file=? AND state!='deleted'",
                        (str(job.output),),
                    ).fetchone()
                capture_id = (
                    row["id"]
                    if row
                    else core.add_capture(
                        job.project, job.epic, audio=str(job.output), source="meeting"
                    )
                )
                self.queue_transcription(capture_id, job, 1)
            except Exception as error:
                self.status.setText(f"No se pudo recuperar una grabación: {str(error)[:120]}")
        for capture in core.list_captures():
            path = capture["audio_file"]
            if (
                capture["state"] == "captured"
                and not capture["transcript"]
                and path
                and Path(path).is_file()
            ):
                self.queue_transcription(capture["id"], Path(path), 1)
        self.refresh()

    @staticmethod
    def format_eta(seconds):
        seconds = max(1, round(seconds))
        if seconds < 60:
            return f"~{seconds} s"
        minutes = max(1, round(seconds / 60))
        if minutes < 60:
            return f"~{minutes} min"
        return f"~{minutes // 60} h {minutes % 60:02d} min"

    def update_resources(self):
        now = time.monotonic()
        if now - self.resource_last_sample < 0.9:
            return
        self.resource_last_sample = now
        if self.resource_system is None:
            self.system_resource_label.setText("CPU -- | RAM --")
        else:
            try:
                cpu = self.resource_system.cpu_percent(None)
                memory = self.resource_system.virtual_memory()
                self.system_resource_label.setText(f"CPU {cpu:.0f}% | RAM {memory.percent:.0f}%")
                self.system_resource_label.setToolTip(
                    f"Uso del equipo: RAM {memory.used / 1024**3:.1f} / {memory.total / 1024**3:.1f} GiB."
                )
            except Exception:
                self.system_resource_label.setText("CPU -- | RAM --")

    def update_daily_activity(self):
        try:
            tickets, tokens = core.daily_activity()
            backend = self.backend.currentText()
            token_text = (
                f"Ollama {tokens:,} tok"
                if backend == "ollama" and tokens is not None
                else f"{backend.title()} tok --"
            )
            self.daily_activity_label.setText(f"Jira hoy {tickets} | {token_text}")
        except Exception:
            self.daily_activity_label.setText("Jira hoy -- | Tokens --")

    def update_progress(self):
        active = (
            bool(self.pending)
            or self.manual_transcribing
            or self.migrating
            or self.importing
            or self.busy
        )
        self.progress_panel.setVisible(active)
        progress = None
        if self.mixing_id is not None:
            message = f"Mezclando audio #{self.mixing_id}"
            progress = self.mix_progress
        elif self.importing:
            message = "Extrayendo audio del archivo"
            progress = self.import_progress
        elif self.migrating:
            message = "Copiando datos…"
        elif self.running_id is not None:
            message = f"Transcribiendo #{self.running_id}"
            progress = self.asr_progress
        elif self.pending:
            message = f"{len(self.pending)} en espera"
        elif self.manual_transcribing:
            message = "Transcribiendo audio"
            progress = self.manual_progress
        elif self.busy:
            message = self.status.text()
        else:
            message = ""
        if progress is not None:
            position, total, elapsed = progress
            if total > 0 and position > 0:
                fraction = max(0.0, min(0.99, position / total))
                percent = int(fraction * 100)
                self.progress.setRange(0, 100)
                self.progress.setValue(percent)
                message += f" | {percent}% del audio"
                if position >= 5 and elapsed >= 3 and fraction < 0.98:
                    message += f" | faltan {self.format_eta(elapsed * (1 - fraction) / fraction)}"
                else:
                    message += " | calculando tiempo…"
            else:
                self.progress.setRange(0, 0)
                message += " | analizando…"
        else:
            self.progress.setRange(0, 0)
            if (
                self.running_id is not None
                and self.mixing_id is None
                and not self.importing
                and not self.migrating
            ):
                message += " | cargando modelo o analizando…"
        self.progress_label.setText("" if message == self.status.text() else message)
        capture_id = self.running_id if self.running_id is not None else self.manual_capture_id
        capture = self.get_capture(capture_id) if capture_id is not None else None
        cancel = (
            self.cancel_events.get(capture_id)
            if self.running_id is not None
            else self.manual_cancel
        )
        can_cancel = bool(capture and cancel is not None and self.mixing_id is None)
        self.cancel_transcription_button.setVisible(can_cancel)
        self.cancel_transcription_button.setEnabled(can_cancel and not cancel.is_set())
        if can_cancel and cancel.is_set():
            self.progress_label.setText("Cancelando transcripción…")
        if active:
            self.update_resources()

    def background(self, function, callback, status, context=None):
        if self.busy:
            return
        self.background_context = context
        self.busy = True
        self.note.setReadOnly(True)
        self.record_button.setEnabled(False)
        self.source.setEnabled(False)
        self.status.setText(status)
        self.update_progress()

        def worker():
            try:
                self.results.put(("job_done", callback, function()))
            except core.TranscriptionCancelled:
                self.results.put(("job_cancelled", callback, None))
            except core.PublicationCancelled:
                self.results.put(("job_cancelled", callback, "publicación"))
            except Exception as error:
                self.results.put(
                    (
                        "job_error",
                        callback,
                        {"message": str(error), "details": traceback.format_exc()},
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        if (
            self.deferred_failures
            and not QApplication.activeModalWidget()
            and not self.failure_windows
        ):
            self.show_media_failure(*self.deferred_failures.pop(0))
        if self.recording:
            recorders = (
                (self.recorder.microphone, self.recorder.system)
                if self.recording_source == "system" and hasattr(self.recorder, "microphone")
                else (self.recorder,)
            )
            if (
                not self.record_confirmation_pending
                and not self.audio_write_error_reported
                and any(
                    getattr(recorder, "write_error", None) is not None for recorder in recorders
                )
            ):
                self.audio_write_error_reported = True
                self.stop_record()
            if not self.recording:
                return
            level = min(100, int(getattr(self.recorder, "level", 0) * 350))
            self.meter.setValue(level)
            self.waveform.set_level(level, True)
            if self.popup.isVisible():
                self.popup.update_recording(level)
        if self.instance:
            for command in self.instance.receive():
                if command == "capture":
                    self.shortcut_capture()
                elif command == "capture-system":
                    self.shortcut_capture("system")
                elif command == "open-history":
                    self.shortcut_open_requested()
                else:
                    self.show_main()
        while True:
            try:
                event, key, value = self.results.get_nowait()
            except queue.Empty:
                break
            if event == "global_shortcuts":
                self.shortcut_lookup_running = False
                self.display_shortcut_summary(value)
                continue
            if event == "record_stopped":
                self.finish_stop_record(key, value)
                continue
            if event == "mix_start":
                self.mixing_id = key
                self.mix_progress = None
                self.update_progress()
                self.refresh()
                continue
            if event == "mix_progress":
                if self.mixing_id == key:
                    self.mix_progress = value
                    self.update_progress()
                continue
            if event == "import_progress":
                if (
                    self.importing
                    and self.background_context
                    and self.background_context.get("operation") == key
                ):
                    self.import_progress = value
                    self.update_progress()
                continue
            if event == "asr_start":
                if self.mixing_id == key:
                    self.mixing_id = None
                    self.mix_progress = None
                self.running_id = key
                self.asr_progress = None
                self.update_progress()
                self.refresh()
                continue
            if event == "asr_progress":
                if self.running_id == key:
                    self.asr_progress = value
                    self.update_progress()
                continue
            if event == "manual_progress":
                if self.manual_transcribing:
                    self.manual_progress = value
                    self.update_progress()
                continue
            if event == "approve_publish":
                request = key
                plan = value
                try:
                    request["accepted"] = self.confirm_publish_plan(plan)
                finally:
                    request["ready"].set()
                continue
            if event in ("asr_done", "asr_error", "asr_cancelled", "mix_error"):
                cancel = self.cancel_events.pop(key, None)
                if cancel is not None and cancel.is_set() and event != "mix_error":
                    event = "asr_cancelled"
                self.pending.discard(key)
                if self.mixing_id == key:
                    self.mixing_id = None
                    self.mix_progress = None
                if self.running_id == key:
                    self.running_id = None
                    self.asr_progress = None
                self.update_progress()
                payload = value if isinstance(value, dict) else {}
                operation = payload.get("operation")
                if event == "asr_cancelled":
                    try:
                        core.cancel_capture_transcription(key)
                        core.finish_operation(operation)
                        self.status.setText(
                            f"Transcripción #{key} cancelada; el audio se conserva en Historial."
                        )
                    except Exception:
                        self.show_media_failure(
                            "No se pudo guardar el estado",
                            "El audio se conserva, pero no se pudo terminar de registrar la cancelación.",
                            traceback.format_exc(),
                        )
                    self.refresh()
                    if (
                        self.close_after_recording
                        and not self.pending
                        and not self.busy
                        and not self.recording
                    ):
                        self.close_after_recording = False
                        if not self.failure_windows:
                            self.quit_requested = True
                            self.close()
                    continue
                if event in ("asr_error", "mix_error"):
                    reason = payload.get("message", str(value))
                    details = payload.get("details", reason)
                    self.asr_errors[key] = reason
                    message = (
                        "No se pudo preparar la grabación. Las dos pistas se conservaron; podés reintentar desde Historial."
                        if event == "mix_error"
                        else "No se pudo transcribir la nota. El audio se conserva; podés reintentar desde Historial."
                    )
                    try:
                        core.save_capture_error(key, event, message, f"Nota: #{key}\n{details}")
                        core.finish_operation(operation)
                    except Exception:
                        details += "\nNo se pudo guardar el diagnóstico:\n" + traceback.format_exc()
                    self.status.setText(
                        f"No se pudo procesar #{key}; revisá el aviso y el Historial."
                    )
                    self.show_media_failure(
                        "No se pudo procesar el audio", message, f"Nota: #{key}\n{details}"
                    )
                else:
                    value = payload.get("text", value)
                    edited = self.capture_id == key and bool(self.note.toPlainText().strip())
                    try:
                        saved = not edited and core.save_automatic_transcript(key, value)
                        if saved and not value.strip():
                            message = "El audio se guardó, pero no se detectó voz. Revisá la fuente o probá otro modelo."
                            details = f"Nota: #{key}\nEl motor no devolvió segmentos de voz."
                            core.save_capture_error(key, "sin_voz", message, details)
                            self.status.setText(f"Audio #{key} guardado; no se detectó voz.")
                            self.show_media_failure("No se detectó voz", message, details)
                        else:
                            existing = core.get_capture_error(key)
                            if not existing or existing["kind"] != "audio_overflow":
                                core.clear_capture_error(key)
                        core.finish_operation(operation)
                    except Exception:
                        details = traceback.format_exc()
                        self.status.setText(
                            f"No se pudo guardar la transcripción #{key}; el audio está en Historial."
                        )
                        self.show_media_failure(
                            "No se pudo guardar la transcripción",
                            "El audio se conserva. Revisá los detalles y volvé a intentarlo.",
                            f"Nota: #{key}\n{details}",
                        )
                        if key == self.quick_id:
                            self.popup.transcription_finished(False)
                        self.refresh()
                        continue
                    if saved and self.capture_id == key and not self.recording:
                        self.note.setPlainText(value)
                    if saved and value.strip() and not self.recording and not self.busy:
                        self.status.setText(f"Transcripción #{key} guardada.")
                if key == self.quick_id:
                    self.popup.transcription_finished(event == "asr_done" and bool(value) and saved)
                    if event == "mix_error":
                        self.popup.message.setText(
                            "No se pudo preparar. Las pistas siguen guardadas; reintentá en Historial."
                        )
                self.refresh()
                if (
                    self.close_after_recording
                    and not self.pending
                    and not self.busy
                    and not self.recording
                ):
                    self.close_after_recording = False
                    if event == "asr_done" and bool(value) and saved and not self.failure_windows:
                        self.quit_requested = True
                        self.close()
                continue
            context = self.background_context
            self.background_context = None
            if (
                context
                and context.get("kind") == "transcripción manual"
                and self.manual_cancel
                and self.manual_cancel.is_set()
            ):
                event = "job_cancelled"
            self.manual_cancel = None
            self.manual_capture_id = None
            self.busy = False
            self.manual_transcribing = False
            self.manual_progress = None
            self.importing = False
            self.import_progress = None
            self.update_progress()
            self.note.setReadOnly(False)
            self.record_button.setEnabled(True)
            self.source.setEnabled(not self.recording)
            if context and context.get("kind") == "publicación" and self.review_id is not None:
                editable = core.get_ticket(self.review_id)["state"] == "draft"
                self.review_title.setReadOnly(not editable)
                self.review_body.setReadOnly(not editable)
                self.review_epic.setEnabled(editable)
                self.review_type.setEnabled(editable)
                self.send.setEnabled(editable)
                for editor, _ in self.review_field_editors.values():
                    editor.setEnabled(editable)
            if self.migrating:
                self.migrating = False
                self.update_progress()
                self.pages.setEnabled(True)
                for nav in self.nav.values():
                    nav.setEnabled(True)
            if event == "job_cancelled":
                try:
                    if context:
                        core.finish_operation(context.get("operation"))
                    self.status.setText(
                        "Envío cancelado; no se creó el ticket."
                        if value == "publicación"
                        else "Transcripción cancelada; se conservan el audio y el texto anterior."
                    )
                except Exception:
                    self.show_media_failure(
                        "No se pudo guardar el estado",
                        "El audio se conserva, pero no se pudo terminar de registrar la cancelación.",
                        traceback.format_exc(),
                    )
                self.refresh()
            elif event == "job_error":
                if self.play_cancel is not None:
                    self.playing = False
                    self.play_cancel = None
                    self.play_button.setText("Escuchar audio")
                if (
                    self.review_id is not None
                    and core.get_ticket(self.review_id)["state"] != "draft"
                ):
                    self.send.setEnabled(False)
                    self.review_info.setText(
                        "Resultado incierto: revisá Jira antes de intentar de nuevo."
                    )
                payload = (
                    value
                    if isinstance(value, dict)
                    else {"message": str(value), "details": str(value)}
                )
                if context and context["kind"] in ("importación", "transcripción manual"):
                    message = (
                        "No se pudo importar el archivo. Revisá que tenga una pista de audio compatible; el original no se modificó."
                        if context["kind"] == "importación"
                        else "No se pudo transcribir la nota. El audio se conserva; podés reintentar desde Historial."
                    )
                    details = (
                        f"Operación: {context['kind']}\n"
                        + (f"Archivo original: {context['path']}\n" if context.get("path") else "")
                        + payload["details"]
                    )
                    try:
                        if context.get("capture_id") is not None:
                            core.save_capture_error(
                                context["capture_id"], "transcripción", message, details
                            )
                        core.finish_operation(context.get("operation"))
                    except Exception:
                        details += "\nNo se pudo guardar el diagnóstico:\n" + traceback.format_exc()
                    self.status.setText("No se pudo procesar el audio; revisá el aviso.")
                    self.show_media_failure("No se pudo procesar el audio", message, details)
                else:
                    self.error("Operación fallida", payload["message"])
                self.refresh()
            else:
                try:
                    key(value)
                except Exception:
                    if context and context["kind"] in ("importación", "transcripción manual"):
                        self.show_media_failure(
                            "No se pudo guardar el resultado",
                            "El audio original se conserva. Revisá los detalles.",
                            traceback.format_exc(),
                        )
                    else:
                        self.error("Operación fallida", traceback.format_exc())
                    self.refresh()
            if self.close_after_playback:
                self.close_after_playback = False
                self.close()

    @staticmethod
    def selected_ids(widget):
        return [item.data(Qt.ItemDataRole.UserRole) for item in widget.selectedItems()]

    @staticmethod
    def _snapshot(widget):
        item = widget.currentItem()
        return (
            set(VoiceWindow.selected_ids(widget)),
            (item.data(Qt.ItemDataRole.UserRole) if item else None),
            widget.verticalScrollBar().value(),
        )

    @staticmethod
    def _restore(widget, snapshot):
        selected, current, scroll = snapshot
        for index in range(widget.count()):
            item = widget.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == current:
                widget.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) in selected)
        widget.verticalScrollBar().setValue(scroll)

    def refresh(self):
        selected_notes = self._snapshot(self.history)
        selected_tickets = self._snapshot(self.tickets)
        errors = core.list_capture_errors()
        self.history.clear()
        self.tickets.clear()
        for c in core.list_captures():
            problem = errors.get(c["id"])
            text = (
                "Transcribiendo"
                if c["id"] == self.running_id
                else "En espera"
                if c["id"] in self.pending
                else "Revisar error"
                if problem
                else "Texto guardado"
                if c["transcript"].strip()
                else "Transcripción cancelada"
                if c["state"] == "cancelled"
                else "Sin transcripción"
            )
            audio = (
                "Audio guardado"
                if c["audio_file"] and Path(c["audio_file"]).is_file()
                else "Preparando audio"
                if c["id"] in self.pending and c["audio_source"] == "meeting"
                else "Pistas guardadas"
                if c["audio_source"] == "meeting" and self.mix_job_for_output(c["audio_file"])
                else "Sin audio"
            )
            if c["audio_source"] in ("mic", "system", "meeting", "file"):
                audio += (
                    " / "
                    + {
                        "mic": "Micrófono",
                        "system": "Equipo",
                        "meeting": "Reunión",
                        "file": "Archivo",
                    }[c["audio_source"]]
                )
            item = QListWidgetItem(
                f"#{c['id']} | {c['created_at'][:16].replace('T', ' ')} | {audio} | {text} | {c['transcript'][:60].replace(chr(10), ' ')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, c["id"])
            self.history.addItem(item)
        self._restore(self.history, selected_notes)
        self.update_error_details_button()
        states = {
            "draft": "Borrador",
            "published": "Creado",
            "uncertain": "Revisar en Jira",
            "sending": "Revisar en Jira",
        }
        for ticket in core.list_tickets():
            item = QListWidgetItem(
                f"{states.get(ticket['state'], ticket['state'])} | {ticket['jira_key'] or ticket['epic_key'] or ticket['project_key']} | {ticket['title']}"
            )
            item.setData(Qt.ItemDataRole.UserRole, ticket["id"])
            self.tickets.addItem(item)
        self._restore(self.tickets, selected_tickets)

    @staticmethod
    def mix_job_for_output(output):
        if not output:
            return None
        for manifest in core.AUDIO_DIR.glob("mix-*.json"):
            try:
                job = read_mix_job(manifest)
                if job.output == Path(output):
                    return job
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def get_capture(self, capture_id):
        with core.db_connect() as db:
            return db.execute(
                "SELECT * FROM captures WHERE id=? AND state != 'deleted'", (capture_id,)
            ).fetchone()

    def open_capture(self):
        if self.busy or self.recording:
            self.info("Operación en curso", "Esperá a que termine la operación actual.")
            return False
        ids = self.selected_ids(self.history)
        if len(ids) != 1:
            self.info("Elegí una nota", "Seleccioná una sola nota para abrirla.")
            return False
        if self.note.toPlainText().strip() and self.save_note() is None:
            return False
        cap = self.get_capture(ids[0])
        if cap is None:
            self.refresh()
            return False
        self.capture_id = cap["id"]
        self.project.setCurrentText(cap["project_key"])
        self.fill_epics()
        self.epic.setCurrentText(cap["epic_key"] or "Sin épica")
        self.note.setPlainText(cap["transcript"])
        self.show_page(0)
        self.status.setText(f"Nota #{cap['id']} abierta.")
        return True

    def retranscribe(self):
        if self.busy or self.recording:
            self.info("Operación en curso", "Esperá a que termine la operación actual.")
            return
        if self.pending:
            self.info(
                "Transcripción en curso", "Esperá a que termine o cancelá la transcripción actual."
            )
            return
        ids = self.selected_ids(self.history)
        if len(ids) != 1:
            self.info("Elegí una nota", "Seleccioná una sola nota.")
            return
        if ids[0] in self.pending:
            self.info("En curso", "La transcripción ya está en proceso.")
            return
        cap = self.get_capture(ids[0])
        if cap and cap["audio_file"] and not Path(cap["audio_file"]).is_file():
            job = self.mix_job_for_output(cap["audio_file"])
            if job:
                self.queue_transcription(cap["id"], job)
                self.refresh()
                self.status.setText("Reintentando preparar la grabación…")
                return
        if not cap or not cap["audio_file"] or not Path(cap["audio_file"]).is_file():
            self.info("Sin audio", "No hay audio disponible.")
            return
        if cap["transcript"] and not self.confirm(
            "Volver a transcribir", "Esto reemplazará el texto guardado. ¿Continuar?"
        ):
            return
        if not self.open_capture():
            return
        capture_id = cap["id"]
        path = Path(cap["audio_file"])
        model = self.whisper_model.currentData()
        language = self.transcription_language.currentData()
        try:
            operation = core.begin_operation(
                "transcripción manual",
                capture_id,
                f"Audio: {path}\nModelo: {model}\nIdioma: {language}",
            )
        except Exception:
            self.show_media_failure(
                "No se pudo transcribir",
                "No fue posible iniciar la transcripción.",
                traceback.format_exc(),
            )
            return

        def done(text):
            if not text.strip():
                self.report_capture_problem(
                    capture_id,
                    "sin_voz",
                    "El audio se conservó, pero no se detectó voz. La transcripción anterior también se conservó.",
                    "El motor no devolvió segmentos de voz.",
                )
                core.finish_operation(operation)
                self.refresh()
                self.status.setText(f"Audio #{capture_id} guardado; no se detectó voz.")
                return
            core.update_capture(capture_id, text)
            core.clear_capture_error(capture_id)
            core.finish_operation(operation)
            if self.capture_id == capture_id:
                self.note.setPlainText(text)
            self.asr_errors.pop(capture_id, None)
            self.refresh()
            self.status.setText(f"Transcripción #{capture_id} guardada.")

        self.manual_transcribing = True
        self.manual_cancel = threading.Event()
        self.manual_capture_id = capture_id
        cancel = self.manual_cancel
        self.update_progress()
        self.background(
            lambda: core.transcribe(
                path,
                model,
                progress=lambda pos, total, elapsed: self.results.put(
                    ("manual_progress", capture_id, (pos, total, elapsed))
                ),
                language=language,
                cancel=cancel,
            ),
            done,
            "Transcribiendo audio…",
            context={
                "kind": "transcripción manual",
                "operation": operation,
                "capture_id": capture_id,
            },
        )

    def delete_captures(self):
        ids = self.selected_ids(self.history)
        if not ids:
            self.info("Elegí notas", "Seleccioná una o más notas.")
            return
        if self.busy or self.recording or any(i in self.pending for i in ids):
            self.info(
                "Operación en curso",
                "Esperá a que terminen las operaciones de las notas seleccionadas.",
            )
            return
        if not self.confirm(
            "Borrar notas",
            f"¿Borrar {len(ids)} nota(s), su texto y audio administrado por la app? Los tickets se conservan.",
        ):
            return
        errors = []
        removed = []
        preserved = False
        for capture_id in ids:
            try:
                cap = self.get_capture(capture_id)
                job = self.mix_job_for_output(cap["audio_file"]) if cap else None
                preserved |= core.delete_capture(
                    capture_id, discard_manifest=job.manifest if job else None
                )
                removed.append(capture_id)
            except Exception as error:
                if self.get_capture(capture_id) is None:
                    removed.append(capture_id)
                    errors.append(
                        f"#{capture_id}: nota borrada, limpieza del audio pendiente: {error}"
                    )
                else:
                    errors.append(f"#{capture_id}: no se pudo borrar: {error}")
        if self.capture_id in removed:
            self.capture_id = None
            self.note.clear()
        self.refresh()
        self.status.setText(f"{len(removed)} de {len(ids)} nota(s) borrada(s).")
        if errors:
            self.error("Borrado incompleto", "\n".join(errors))
        if preserved:
            self.info(
                "Audio conservado", "Algunos archivos compartidos o externos pueden conservarse."
            )

    def delete_tickets(self):
        ids = self.selected_ids(self.tickets)
        if not ids:
            self.info("Elegí tickets", "Seleccioná uno o varios tickets.")
            return
        if self.busy or self.recording:
            self.info("Operación en curso", "Esperá a que termine la operación.")
            return
        try:
            tickets = [core.get_ticket(i) for i in ids]
        except ValueError as error:
            self.error("Registro no disponible", error)
            self.refresh()
            return
        if any(t["state"] not in ("draft", "published") for t in tickets):
            self.info(
                "Revisá Jira",
                "La selección incluye un envío incierto o en curso; no se borró ningún registro.",
            )
            return
        if not self.confirm(
            "Borrar registros",
            f"¿Borrar {len(ids)} registro(s) local(es)? Los tickets publicados en Jira y las notas se conservan.",
        ):
            return
        errors = []
        removed = 0
        for ticket_id in ids:
            try:
                core.delete_ticket(ticket_id)
                removed += 1
            except Exception as error:
                errors.append(f"#{ticket_id}: {error}")
        self.refresh()
        self.status.setText(
            f"{removed} de {len(ids)} registro(s) local(es) borrado(s). Jira no se modificó."
        )
        if errors:
            self.error("Algunos registros no se borraron", "\n".join(errors))

    def play_audio(self):
        if self.playing:
            self.play_cancel.set()
            self.playing = False
            self.play_button.setText("Escuchar audio")
            self.status.setText("Reproducción detenida.")
            return
        if self.busy:
            return
        ids = self.selected_ids(self.history)
        if len(ids) != 1:
            self.info("Elegí una nota", "Seleccioná una sola nota.")
            return
        cap = self.get_capture(ids[0])
        if not cap or not cap["audio_file"] or not Path(cap["audio_file"]).is_file():
            self.info("Sin audio", "No hay audio disponible.")
            return
        path = cap["audio_file"]
        cancel = threading.Event()
        self.play_cancel = cancel

        def play():
            import numpy as np
            import sounddevice as sd

            with wave.open(path, "rb") as audio:
                channels = audio.getnchannels()
                rate = audio.getframerate()
                if cancel.is_set():
                    return
                with sd.OutputStream(samplerate=rate, channels=channels, dtype="int16") as stream:
                    while not cancel.is_set():
                        chunk = audio.readframes(1024)
                        if not chunk:
                            break
                        stream.write(np.frombuffer(chunk, dtype="<i2").reshape(-1, channels))

        def finished(_):
            self.playing = False
            self.play_cancel = None
            self.play_button.setText("Escuchar audio")
            if self.status.text() != "Reproducción detenida.":
                self.status.setText("Terminó la reproducción.")

        self.playing = True
        self.play_button.setText("Detener reproducción")
        self.background(play, finished, "Reproduciendo audio…")

    def prepare(self):
        if self.busy or self.recording:
            return
        capture_id = self.save_note()
        if capture_id is None:
            return
        transcript = self.note.toPlainText().strip()
        destination = {
            "site": self.settings["jira_site"].text().strip(),
            "project": self.project.currentText().strip().upper(),
            "epic": self.epic_key() or None,
            "config": core.load_config(),
        }
        if self.backend.currentText() == "cursor":
            model = self.cursor_model.currentText().strip()
            function = partial(core.draft_with_cursor, transcript, model, **destination)
        else:
            model = self.settings["ollama_model"].text().strip()
            function = partial(core.draft_with_ollama, transcript, model, **destination)

        def prepare_with_fields():
            tickets = function()
            failures = []
            for ticket in tickets:
                cached = core.cached_jira_fields(
                    destination["site"], destination["project"], ticket["type"]
                )
                if not cached:
                    continue
                fields, _ = cached
                project_rules, epic_rules = core.jira_instructions(
                    destination["config"],
                    destination["site"],
                    destination["project"],
                    destination["epic"],
                )
                draft = {
                    "title": ticket["title"],
                    "description": ticket["description"],
                    "ticket_type": ticket["type"],
                    "project_key": destination["project"],
                    "epic_key": destination["epic"],
                }
                try:
                    response = core.plan_jira_fields(
                        draft,
                        destination["site"],
                        fields,
                        project_rules,
                        epic_rules,
                        destination["config"],
                        transcript,
                    )
                    proposal = json.loads(
                        response.strip().removeprefix("```json").removesuffix("```").strip()
                    )
                    values = proposal.get("fields", {})
                    allowed = {field.get("id") for field in fields}
                    if not isinstance(values, dict) or any(key not in allowed for key in values):
                        raise ValueError("El modelo devolvió campos no disponibles")
                    standard = proposal.get("standard", {})
                    if not isinstance(standard, dict) or set(standard) - {
                        "assignee",
                        "priority",
                        "labels",
                    }:
                        raise ValueError("El modelo devolvió campos estándar inválidos")
                    values.update({"standard:" + key: value for key, value in standard.items()})
                    ticket["extra_fields"] = values
                    origins = proposal.get("origins", {})
                    ticket["field_origins"] = (
                        {
                            key: origin if origin in ("transcripción", "instrucciones") else "IA"
                            for key, origin in origins.items()
                            if key in values
                        }
                        if isinstance(origins, dict)
                        else {}
                    )
                    ticket["site"] = destination["site"]
                except Exception as error:
                    failures.append(str(error))
            return tickets, failures

        def done(result):
            tickets, failures = result
            count = core.save_drafts(capture_id, tickets)
            self.refresh()
            self.show_page(1)
            self.status.setText(f"{count} borrador(es) guardados en Mis tickets.")
            if not tickets:
                self.info("Sin tickets", "No se encontraron tareas concretas en la nota.")
            elif failures:
                self.info(
                    "Campos sin completar",
                    "Los borradores se guardaron, pero no se pudieron proponer algunos campos. Revisalos antes de enviar.\n\n"
                    + "\n".join(failures),
                )

        self.background(prepare_with_fields, done, "Preparando tickets…")

    def open_ticket(self):
        ids = self.selected_ids(self.tickets)
        if len(ids) != 1:
            self.info("Elegí un ticket", "Seleccioná un solo ticket para revisarlo.")
            return
        try:
            ticket = core.get_ticket(ids[0])
        except ValueError as error:
            self.error("No se pudo abrir", error)
            return
        self.review_fields_type = None
        self.review_id = ticket["id"]
        self.review_title.setText(ticket["title"])
        self.review_body.setPlainText(ticket["description"])
        site = ticket["jira_site"] or self.config.get("jira_site", "")
        self.review_type.blockSignals(True)
        self.review_type.clear()
        self.review_type.addItems(
            core.cached_jira_types(site, ticket["project_key"]) or ["Tarea", "Error", "Historia"]
        )
        self.review_type.blockSignals(False)
        chosen = {"Task": "Tarea", "Bug": "Error", "Story": "Historia"}.get(
            ticket["ticket_type"], ticket["ticket_type"]
        )
        if (
            self.review_type.findText(chosen) < 0
            and self.review_type.findText(ticket["ticket_type"]) >= 0
        ):
            chosen = ticket["ticket_type"]
        if self.review_type.findText(chosen) < 0:
            self.review_type.addItem(chosen)
        self.review_type.setCurrentText(chosen)
        self.load_review_fields(ticket)
        self.fill_review_epics(ticket)
        self.review_info.setText(
            f"Proyecto {ticket['project_key']} | {ticket['state']} | {ticket['jira_key'] or 'Sin enviar'}"
            + (f" | {ticket['error']}" if ticket["error"] else "")
        )
        editable = ticket["state"] == "draft"
        self.review_fields_close_button.setText("Guardar y cerrar" if editable else "Cerrar")
        self.review_title.setReadOnly(not editable)
        self.review_body.setReadOnly(not editable)
        self.review_epic.setEnabled(editable)
        self.review_type.setEnabled(editable)
        self.send.setEnabled(editable)
        self.delete_review.setEnabled(ticket["state"] in ("draft", "published"))
        self.delete_review.setText(
            "Borrar borrador" if ticket["state"] == "draft" else "Quitar registro local"
        )
        self.show_page(4)

    def review_kind(self):
        return {"Tarea": "Task", "Error": "Bug", "Historia": "Story"}.get(
            self.review_type.currentText(), self.review_type.currentText()
        )

    def show_review_fields(self):
        if self.review_id is None:
            return
        self.review_fields_error.hide()
        self.review_fields_dialog.exec()

    def flush_review_fields(self):
        try:
            self.persist_review_fields()
        except Exception as error:
            self.review_fields_error.setText(f"No se pudieron guardar los campos: {error}")
            self.review_fields_error.show()
            return False
        return True

    def review_type_changed(self):
        if self.review_id is None or self.review_fields_type is None:
            return
        try:
            self.persist_review_fields()
            self.load_review_fields(core.get_ticket(self.review_id))
        except (ValueError, json.JSONDecodeError) as error:
            previous = {"Task": "Tarea", "Bug": "Error", "Story": "Historia"}.get(
                self.review_fields_type, self.review_fields_type
            )
            self.review_type.blockSignals(True)
            self.review_type.setCurrentText(previous)
            self.review_type.blockSignals(False)
            self.error("Campos inválidos", str(error))

    def load_review_fields(self, ticket):
        while self.review_fields_layout.count():
            item = self.review_fields_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.review_field_editors = {}
        kind = self.review_kind()
        self.review_fields_type = kind
        site = ticket["jira_site"] or self.config.get("jira_site", "")
        cached = core.cached_jira_fields(site, ticket["project_key"], kind)
        if not cached:
            self.review_fields_status.setText(
                "Sin campos sincronizados. Sincronizalos desde Configuración."
            )
            self.review_fields_description.setText(self.review_fields_status.text())
            return
        fields, synced = cached
        self.review_fields_status.setText(
            f"Sincronizados: {synced}. Los valores se guardan en el borrador."
        )
        self.review_fields_description.setText(self.review_fields_status.text())
        saved = core.ticket_extra_fields(ticket["id"], site, kind)
        self.review_origins = core.ticket_extra_origins(ticket["id"], site, kind)
        standard_fields = [
            {
                "id": "standard:assignee",
                "name": "Responsable (ID o me)",
                "schema": {"type": "string"},
            },
            {
                "id": "standard:priority",
                "name": "Prioridad",
                "schema": {"type": "string"},
            },
            {"id": "standard:labels", "name": "Etiquetas", "schema": {"type": "labels"}},
        ]
        known_ids = {field.get("id") for field in fields}
        removed_fields = [
            {"id": key, "name": f"{key} (ya no disponible)", "schema": {"type": "any"}}
            for key in saved
            if key.startswith("customfield_") and key not in known_ids
        ]
        required_fields = [field for field in fields if field.get("required")]
        internal_types = {
            "com.atlassian.jira.plugins.jira-development-integration-plugin:devsummarycf",
            "com.pyxis.greenhopper.jira:gh-lexo-rank",
        }
        optional_fields = [
            field
            for field in fields
            if not field.get("required")
            and (
                (field.get("schema") or {}).get("custom") not in internal_types
                or saved.get(field.get("id")) not in (None, "", [])
            )
        ]
        optional_container = QWidget()
        optional_layout = QVBoxLayout(optional_container)
        optional_layout.setContentsMargins(0, 0, 0, 0)
        optional_button = button("Ocultar campos opcionales", lambda: None)

        def toggle_optional():
            visible = optional_container.isHidden()
            optional_container.setVisible(visible)
            optional_button.setText(
                "Ocultar campos opcionales" if visible else "Mostrar campos opcionales"
            )

        optional_button.clicked.connect(toggle_optional)
        for field in required_fields + removed_fields + standard_fields + optional_fields:
            field_id = field.get("id", "")
            if not (
                re.fullmatch(r"customfield_\d+", str(field_id)) or field_id.startswith("standard:")
            ):
                continue
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            caption = label(
                str(field.get("name") or field_id) + (" *" if field.get("required") else "")
            )
            base_caption = caption.text()
            origin = self.review_origins.get(field_id)
            if origin:
                caption.setText(
                    f"{base_caption} ({'editado' if origin == 'usuario' else 'IA: ' + origin})"
                )
            caption.setMinimumWidth(160)
            layout.addWidget(caption)
            schema = field.get("schema") or {}
            options = field.get("allowedValues") or []
            value = saved.get(field_id)
            special = core.jira_special_field_kind(field)
            flag_option = (
                options[0]
                if field.get("name", "").casefold() == "flagged"
                and schema.get("type") == "array"
                and len(options) == 1
                and isinstance(options[0], dict)
                and options[0].get("value", "").casefold() == "impediment"
                and options[0].get("id")
                else None
            )
            flag_value = {"id": str(flag_option["id"])} if flag_option else None
            if flag_option and value not in (None, [], [flag_value]):
                flag_option = None
            if field_id == "standard:priority":
                editor = ClickComboBox()
                priorities = core.cached_jira_priorities(site)
                editor.addItem(
                    "Sin seleccionar" if priorities else "Sin opciones; sincronizá campos de Jira",
                    None,
                )
                for option in priorities or []:
                    editor.addItem(option["label"], option["value"])
                index = editor.findData(value)
                if value is not None and index < 0:
                    matches = [
                        i
                        for i in range(1, editor.count())
                        if editor.itemText(i).casefold() == str(value).casefold()
                    ]
                    if len(matches) == 1:
                        index = matches[0]
                    else:
                        editor.addItem(f"{value} (valor guardado; revisar)", value)
                        index = editor.count() - 1
                editor.setCurrentIndex(max(0, index))
                editor.setToolTip(
                    "Prioridades del sitio Jira; el proyecto puede aplicar restricciones adicionales."
                )
                mode = "option"
            elif flag_option:
                caption.setText("Impedimento")
                base_caption = caption.text()
                if origin:
                    caption.setText(
                        f"{base_caption} ({'editado' if origin == 'usuario' else 'IA: ' + origin})"
                    )
                editor = QCheckBox("Marcar el ticket con una bandera")
                editor.setToolTip(
                    "Destaca el ticket en Jira cuando requiere atención o está bloqueado."
                )
                editor.setChecked(value == [flag_value])
                editor.flag_value = flag_value
                mode = "flag"
            elif special:
                editor = ClickComboBox()
                editor.addItem("Sin seleccionar", None)
                for option in field.get("backlogtalk_options", []):
                    editor.addItem(option["label"], option["value"])
                index = editor.findData(value)
                if value is not None and index < 0:
                    editor.addItem("Valor guardado no disponible (revisar)", value)
                    index = editor.count() - 1
                editor.setCurrentIndex(max(0, index))
                if "backlogtalk_options" not in field:
                    editor.setToolTip("Sincronizá los campos de Jira para cargar las opciones.")
                    if value is None:
                        editor.setItemText(0, "Sin opciones; sincronizá campos de Jira")
                elif not field["backlogtalk_options"] and value is None:
                    editor.setItemText(0, "No se encontraron opciones disponibles")
                mode = "option"
            elif options and schema.get("type") == "array":
                editor = QListWidget()
                editor.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
                editor.setMaximumHeight(90)
                selected = value if isinstance(value, list) else []
                for option in options:
                    if isinstance(option, dict):
                        item = QListWidgetItem(
                            str(option.get("value") or option.get("name") or option.get("id"))
                        )
                        option_value = {"id": str(option.get("id"))} if option.get("id") else option
                        item.setData(Qt.ItemDataRole.UserRole, option_value)
                        editor.addItem(item)
                        item.setSelected(option_value in selected)
                available_values = [
                    editor.item(i).data(Qt.ItemDataRole.UserRole) for i in range(editor.count())
                ]
                for old_value in selected:
                    if old_value not in available_values:
                        item = QListWidgetItem("Valor guardado no disponible (revisar)")
                        item.setData(Qt.ItemDataRole.UserRole, old_value)
                        editor.addItem(item)
                        item.setSelected(True)
                mode = "multi"
            elif options:
                editor = ClickComboBox()
                editor.addItem("Sin seleccionar", None)
                for option in options:
                    if isinstance(option, dict):
                        editor.addItem(
                            str(option.get("value") or option.get("name") or option.get("id")),
                            {"id": str(option.get("id"))} if option.get("id") else option,
                        )
                index = editor.findData(value)
                if value is not None and index < 0:
                    editor.addItem("Valor guardado (revisar)", value)
                    index = editor.count() - 1
                editor.setCurrentIndex(max(0, index))
                mode = "option"
            elif schema.get("type") == "date" and (
                value in (None, "") or QDate.fromString(str(value), Qt.DateFormat.ISODate).isValid()
            ):
                editor = QWidget()
                date_row = QHBoxLayout(editor)
                date_row.setContentsMargins(0, 0, 0, 0)
                editor.date_enabled = QCheckBox("Definir")
                editor.date_picker = QDateEdit()
                editor.date_picker.setDisplayFormat("yyyy-MM-dd")
                editor.date_picker.setCalendarPopup(True)
                editor.date_picker.setDate(
                    QDate.fromString(str(value), Qt.DateFormat.ISODate)
                    if value
                    else QDate.currentDate()
                )
                editor.date_enabled.setChecked(bool(value))
                editor.date_picker.setEnabled(bool(value))
                editor.date_enabled.toggled.connect(editor.date_picker.setEnabled)
                date_row.addWidget(editor.date_enabled)
                date_row.addWidget(editor.date_picker, 1)
                mode = "date"
            else:
                editor = QLineEdit()
                kind_name = schema.get("type", "")
                mode = (
                    "labels"
                    if kind_name == "labels"
                    else "number"
                    if kind_name == "number"
                    else "text"
                    if kind_name in ("string", "date", "datetime")
                    else "json"
                )
                editor.setPlaceholderText(
                    {
                        "number": "Número",
                        "labels": "Separadas por coma",
                        "text": "AAAA-MM-DD" if kind_name == "date" else "Texto",
                        "json": "Valor JSON; consultar opciones en Jira",
                    }[mode]
                )
                if value is not None:
                    editor.setText(
                        json.dumps(value, ensure_ascii=False)
                        if mode == "json"
                        else ", ".join(value)
                        if mode == "labels" and isinstance(value, list)
                        else str(value)
                    )
            editor.setEnabled(ticket["state"] == "draft")

            def mark_edited(*_, key=field_id, field_label=caption, base=base_caption):
                self.review_origins[key] = "usuario"
                field_label.setText(base + " (editado)")

            if mode == "option":
                editor.currentIndexChanged.connect(mark_edited)
            elif mode == "multi":
                editor.itemSelectionChanged.connect(mark_edited)
            elif mode == "flag":
                editor.toggled.connect(mark_edited)
            elif mode == "date":
                editor.date_enabled.toggled.connect(mark_edited)
                editor.date_picker.dateChanged.connect(mark_edited)
            else:
                editor.textEdited.connect(mark_edited)
            layout.addWidget(editor, 1)
            target_layout = (
                self.review_fields_layout
                if field in required_fields or field in removed_fields
                else optional_layout
            )
            target_layout.addWidget(row)
            self.review_field_editors[field_id] = (editor, mode)
        self.review_fields_layout.addWidget(optional_button)
        self.review_fields_layout.addWidget(optional_container)
        optional_container.show()

    def persist_review_fields(self):
        if self.review_id is None or self.review_fields_type is None:
            return
        ticket = core.get_ticket(self.review_id)
        if ticket["state"] != "draft" or not self.review_field_editors:
            return
        site = ticket["jira_site"] or self.config.get("jira_site", "")
        values = core.ticket_extra_fields(self.review_id, site, self.review_fields_type)
        for field_id, (editor, mode) in self.review_field_editors.items():
            if mode == "option":
                value = editor.currentData()
            elif mode == "multi":
                value = [item.data(Qt.ItemDataRole.UserRole) for item in editor.selectedItems()]
            elif mode == "flag":
                value = [editor.flag_value] if editor.isChecked() else []
            elif mode == "date":
                value = (
                    editor.date_picker.date().toString(Qt.DateFormat.ISODate)
                    if editor.date_enabled.isChecked()
                    else None
                )
            else:
                text = editor.text().strip()
                if not text:
                    value = None
                elif mode == "json":
                    value = json.loads(text)
                elif mode == "number":
                    value = float(text)
                elif mode == "labels":
                    value = [item.strip() for item in text.split(",") if item.strip()]
                    if any(not re.fullmatch(r"[^,\s]+", item) for item in value):
                        raise ValueError("Las etiquetas no pueden contener espacios")
                else:
                    value = text
            values[field_id] = value
        core.save_ticket_extra_fields(
            self.review_id, site, self.review_fields_type, values, self.review_origins
        )

    def fill_review_epics(self, ticket):
        site = ticket["jira_site"] or self.config.get("jira_site", "")
        scope = core.epic_favorites(self.config, site)
        project = ticket["project_key"]
        names = scope.get("names", {})
        keys = list(scope.get("projects", {}).get(project, []))
        current = ticket["epic_key"] or ""
        if current and current not in keys:
            keys.append(current)
        self.review_epic.clear()
        self.review_epic.addItem("Sin épica", "")
        for key in keys:
            self.review_epic.addItem(f"{names[key]} / {key}" if names.get(key) else key, key)
        self.review_epic.setCurrentIndex(max(0, self.review_epic.findData(current)))

    def update_open_jira_button(self):
        ids = self.selected_ids(self.tickets)
        enabled = False
        tooltip = "Abrir el ticket en Jira"
        if len(ids) == 1:
            try:
                ticket = core.get_ticket(ids[0])
                enabled = bool(ticket["jira_key"])
                if enabled and not ticket["jira_site"]:
                    tooltip = (
                        "Este registro antiguo se abrirá en el sitio Jira configurado actualmente."
                    )
            except ValueError:
                pass
        self.open_jira_button.setEnabled(enabled)
        self.open_jira_button.setToolTip(tooltip)

    def open_selected_jira_ticket(self):
        ids = self.selected_ids(self.tickets)
        if len(ids) != 1:
            return
        ticket = core.get_ticket(ids[0])
        site = ticket["jira_site"] or self.config.get("jira_site", "")
        key = ticket["jira_key"] or ""
        self.open_jira_ticket(site, key)

    def open_jira_ticket(self, site, key):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", site) or not re.fullmatch(
            r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", key
        ):
            self.info(
                "No se pudo abrir Jira", "Revisá el sitio Jira configurado y la clave del ticket."
            )
            return
        if not QDesktopServices.openUrl(QUrl(f"https://{site}/browse/{key}")):
            self.info("No se pudo abrir Jira", "No se pudo abrir el navegador predeterminado.")

    def save_review(self):
        if self.review_id is None:
            return True
        try:
            ticket = core.get_ticket(self.review_id)
            if ticket["state"] != "draft":
                return True
            title = self.review_title.text().strip()
            description = self.review_body.toPlainText().strip()
            if not 5 <= len(title) <= 220 or not description:
                self.error(
                    "Revisá el ticket",
                    "El título debe tener entre 5 y 220 caracteres y la descripción no puede estar vacía.",
                )
                return False
            self.persist_review_fields()
            core.edit_ticket(
                self.review_id,
                title,
                description,
                {"Tarea": "Task", "Error": "Bug", "Historia": "Story"}.get(
                    self.review_type.currentText(), self.review_type.currentText()
                ),
                self.review_epic.currentData() or "",
            )
            self.refresh()
            self.status.setText("Cambios del borrador guardados.")
            return True
        except Exception as error:
            self.error("No se pudo guardar", error)
            return False

    def delete_review_ticket(self):
        if self.review_id is None or self.busy:
            return
        ticket = core.get_ticket(self.review_id)
        if ticket["state"] not in ("draft", "published"):
            self.info("Revisá Jira", "No se puede quitar un envío incierto.")
            return
        question = (
            "¿Borrar este borrador local?"
            if ticket["state"] == "draft"
            else "¿Quitar el registro local? El ticket en Jira se conserva."
        )
        if not self.confirm("Confirmar", question):
            return
        try:
            core.delete_ticket(self.review_id)
        except Exception as error:
            self.error("No se pudo borrar", error)
            return
        self.review_id = None
        self.refresh()
        self.show_page(1)
        self.status.setText("Registro local borrado. Jira no se modificó.")

    def publish(self):
        if self.busy or self.review_id is None or not self.save_review():
            return
        ticket = core.get_ticket(self.review_id)
        if ticket["state"] != "draft":
            self.info("Revisá Jira", "Este ticket no está en borrador.")
            return
        site = self.settings["jira_site"].text().strip()

        def approve(plan):
            request = {"ready": threading.Event(), "accepted": False}
            self.results.put(("approve_publish", request, plan))
            request["ready"].wait()
            return request["accepted"]

        def done(result):
            self.review_id = None
            self.refresh()
            self.update_daily_activity()
            self.show_page(1)
            self.status.setText(result[1] or f"Ticket {result[0]} creado y verificado.")
            dialog, actions = self.notice_dialog(
                "Ticket creado",
                result[0] + (f"\n\n{result[1]}" if result[1] else ""),
            )

            def open_created_ticket():
                dialog.accept()
                self.open_jira_ticket(site, result[0])

            actions.addStretch()
            actions.addWidget(button("Cerrar", dialog.accept))
            actions.addWidget(button("Abrir en Jira", open_created_ticket, "primary"))
            self.show_notice(dialog)

        self.review_title.setReadOnly(True)
        self.review_body.setReadOnly(True)
        self.review_epic.setEnabled(False)
        self.review_type.setEnabled(False)
        self.send.setEnabled(False)
        for editor, _ in self.review_field_editors.values():
            editor.setEnabled(False)
        self.background(
            lambda: core.publish_ticket(ticket["id"], site, approve_plan=approve),
            done,
            "Preparando envío a Jira…",
            context={"kind": "publicación"},
        )

    def confirm_publish_plan(self, plan):
        dialog = QDialog(self)
        dialog.setObjectName("instructionsDialog")
        dialog.setWindowTitle("Confirmar envío a Jira")
        dialog.resize(720, 480)
        box = QVBoxLayout(dialog)
        box.addWidget(
            label("Revisá los valores propuestos antes de crear el ticket.", "sectionTitle")
        )
        details = {
            "Sitio": plan["site"],
            "Proyecto": plan["project"],
            "Épica": plan["epic"] or "Sin épica",
            "Tipo": plan["type"],
            "Título": plan["title"],
            "Descripción": plan.get("description", ""),
            "Campos": plan["fields"],
            "Otros valores": plan["standard"],
            "Comentario": plan["comment"] or "Sin comentario",
        }
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(details, ensure_ascii=False, indent=2))
        box.addWidget(text, 1)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Cancelar", dialog.reject))
        row.addWidget(button("Crear ticket", dialog.accept, "primary"))
        box.addLayout(row)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def load_jira_types(self):
        if self.review_id is None or self.busy:
            return
        ticket = core.get_ticket(self.review_id)
        site = self.settings["jira_site"].text().strip()

        def done(types):
            current = self.review_type.currentText()
            self.review_type.clear()
            self.review_type.addItems(types)
            if self.review_type.findText(current) < 0:
                self.review_type.addItem(current)
            self.review_type.setCurrentText(current)
            self.status.setText("Tipos disponibles en Jira actualizados.")

        self.background(
            lambda: core.jira_types(ticket["project_key"], site), done, "Consultando tipos de Jira…"
        )

    def show_agent_response(self):
        if self.review_id is None:
            return
        ticket = core.get_ticket(self.review_id)
        dialog = QDialog(self)
        dialog.setObjectName("instructionsDialog")
        dialog.setWindowTitle("Salida del envío")
        dialog.resize(720, 520)
        box = QVBoxLayout(dialog)
        box.addWidget(
            label(
                "Respuestas guardadas en este equipo; los detalles pueden contener información del ticket.",
                "muted",
            )
        )
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            "AGENTE\n"
            + (ticket["agent_response"] or "Sin respuesta guardada.")
            + "\n\nTWG\n"
            + (ticket["twg_output"] or "Sin salida guardada.")
            + ("\n\nESTADO\n" + ticket["error"] if ticket["error"] else "")
        )
        box.addWidget(text)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(
            button("Copiar", lambda: QApplication.clipboard().setText(text.toPlainText()))
        )
        row.addWidget(button("Cerrar", dialog.accept))
        box.addLayout(row)
        dialog.exec()

    def edit_jira_context(self):
        if self.settings_dirty and not self.save_settings():
            return
        try:
            core.load_skill()
        except Exception as error:
            self.error("No se pudieron abrir las instrucciones", error)
            return

        class InstructionsDialog(QDialog):
            def flush(self):
                return True

            def accept(self):
                if self.flush():
                    super().accept()

            def reject(self):
                if self.flush():
                    super().reject()

            def closeEvent(self, event):
                if self.flush():
                    super().closeEvent(event)
                else:
                    event.ignore()

        dialog = InstructionsDialog(self)
        dialog.setObjectName("instructionsDialog")
        dialog.setWindowTitle("Gestionar instrucciones")
        dialog.resize(900, 650)
        box = QVBoxLayout(dialog)
        box.addWidget(
            label(
                "Las instrucciones generales se combinan con las del proyecto y la épica seleccionados. Los cambios se guardan automáticamente.",
                "muted",
            )
        )
        body = QHBoxLayout()
        box.addLayout(body, 1)
        scopes = QListWidget()
        scopes.setMinimumWidth(250)
        scopes.setMaximumWidth(330)
        scopes.setWordWrap(True)
        scopes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.addWidget(scopes)
        editor_box = QVBoxLayout()
        body.addLayout(editor_box, 1)
        heading = label("", "sectionTitle")
        editor_box.addWidget(heading)
        description = label("", "muted")
        editor_box.addWidget(description)
        editor = QTextEdit()
        editor_box.addWidget(editor, 1)
        row = QHBoxLayout()
        remove = button("Eliminar instrucciones", lambda: remove_scope(), "danger")
        row.addWidget(remove)
        row.addStretch()
        editor_box.addLayout(row)
        box.addWidget(label("AGREGAR PROYECTO O ÉPICA", "eyebrow"))
        row = QHBoxLayout()
        site = QLineEdit(self.settings["jira_site"].text().strip())
        site.setPlaceholderText("Sitio Jira")
        project = QLineEdit(self.project.currentText().strip().upper())
        project.setPlaceholderText("Proyecto, ej. DEMO")
        epic = QLineEdit(self.epic_key())
        epic.setPlaceholderText("Épica opcional, ej. DEMO-123")
        for field in (site, project, epic):
            row.addWidget(field, 1)
        row.addWidget(button("Agregar", lambda: add_scope()))
        box.addLayout(row)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(button("Guardar y cerrar", dialog.accept, "primary"))
        footer.addStretch()
        box.addLayout(footer)
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.setInterval(800)
        state = {"current": None, "saved": "", "switching": False}

        def save_current():
            timer.stop()
            scope = state["current"]
            text = editor.toPlainText()
            if scope is None or text == state["saved"]:
                return True
            try:
                if scope[0] == "general":
                    core.save_skill(text)
                    if self.pages.currentIndex() != 5:
                        self.skill.blockSignals(True)
                        self.skill.setPlainText(text)
                        self.skill.blockSignals(False)
                        self.skill_saved = text
                else:
                    _, host, key, ekey = scope
                    config = core.load_config()
                    entry = (
                        config.setdefault("jira_contexts", {})
                        .setdefault(host, {})
                        .setdefault(key, {})
                    )
                    if ekey:
                        entry.setdefault("epics", {})[ekey] = text
                    else:
                        entry["instructions"] = text
                    core.save_config(config)
                    self.config = config
                state["saved"] = text
                self.status.setText("Instrucciones guardadas automáticamente.")
                return True
            except Exception as error:
                self.error("No se pudieron guardar las instrucciones", error)
                return False

        dialog.flush = save_current
        timer.timeout.connect(save_current)
        editor.textChanged.connect(lambda: timer.start() if not state["switching"] else None)

        def load_selected():
            if state["switching"]:
                return
            item = scopes.currentItem()
            selected = item.data(Qt.ItemDataRole.UserRole) if item else None
            selected = tuple(selected) if selected else None
            if selected == state["current"]:
                return
            if not save_current():
                state["switching"] = True
                for i in range(scopes.count()):
                    if tuple(scopes.item(i).data(Qt.ItemDataRole.UserRole)) == state["current"]:
                        scopes.setCurrentRow(i)
                        break
                state["switching"] = False
                return
            state["switching"] = True
            state["current"] = selected
            if selected and selected[0] == "general":
                text = core.load_skill()
                heading.setText("Instrucciones generales")
                description.setText("Formato y reglas comunes para todos los tickets.")
                remove.setText("Restaurar plantilla")
            elif selected:
                _, host, key, ekey = selected
                a, b = core.jira_instructions(self.config, host, key, ekey)
                text = b if ekey else a
                heading.setText(f"Épica {ekey}" if ekey else f"Proyecto {key}")
                description.setText(f"{host} / {key}" + (f" / {ekey}" if ekey else ""))
                remove.setText("Eliminar instrucciones")
            else:
                text = ""
                heading.setText("Elegí una instrucción")
                description.clear()
            editor.setPlainText(text)
            state["saved"] = editor.toPlainText()
            editor.setEnabled(bool(selected))
            remove.setEnabled(bool(selected))
            state["switching"] = False

        def refresh_scopes(select=None):
            state["switching"] = True
            scopes.clear()
            entries = [("General / todos los tickets", ("general", "", "", ""))]
            for host, projects in sorted(self.config.get("jira_contexts", {}).items()):
                for key, entry in sorted(projects.items()):
                    entries.append((f"Proyecto {key}\n{host}", ("project", host, key, "")))
                    for ekey in sorted(entry.get("epics", {})):
                        entries.append((f"Épica {ekey}\n{host} / {key}", ("epic", host, key, ekey)))
            index = 0
            for i, (title, scope) in enumerate(entries):
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, scope)
                scopes.addItem(item)
                if scope == select:
                    index = i
            scopes.setCurrentRow(index)
            state["switching"] = False
            state["current"] = None
            load_selected()

        def add_scope():
            host = site.text().strip().lower()
            key = project.text().strip().upper()
            ekey = epic.text().strip().upper()
            if (
                not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                or (ekey and not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", ekey))
            ):
                self.error("Datos inválidos", "Revisá el sitio, proyecto y clave de épica.")
                return
            if not save_current():
                return
            try:
                config = core.load_config()
                entry = (
                    config.setdefault("jira_contexts", {}).setdefault(host, {}).setdefault(key, {})
                )
                if ekey:
                    entry.setdefault("epics", {}).setdefault(ekey, "")
                else:
                    entry.setdefault("instructions", "")
                core.save_config(config)
                self.config = config
            except Exception as error:
                self.error("No se pudo guardar", error)
                return
            refresh_scopes(("epic" if ekey else "project", host, key, ekey))

        def remove_scope():
            scope = state["current"]
            if not scope:
                return
            if scope[0] == "general":
                if self.confirm("Restaurar instrucciones", "¿Restaurar la plantilla general?"):
                    editor.setPlainText(core.load_default_skill())
                    save_current()
                return
            _, host, key, ekey = scope
            question = (
                f"¿Eliminar las instrucciones de la épica {ekey}?"
                if ekey
                else f"¿Eliminar las instrucciones del proyecto {key} y de todas sus épicas?"
            )
            if not self.confirm("Eliminar instrucciones", question):
                return
            try:
                config = core.load_config()
                projects = config["jira_contexts"][host]
                if ekey:
                    projects[key].get("epics", {}).pop(ekey, None)
                else:
                    projects.pop(key, None)
                if not projects:
                    config["jira_contexts"].pop(host, None)
                core.save_config(config)
                self.config = config
                timer.stop()
                state["current"] = None
                refresh_scopes()
            except Exception as error:
                self.error("No se pudieron eliminar", error)

        scopes.currentItemChanged.connect(lambda *_: load_selected())
        refresh_scopes()
        dialog.exec()

    def open_skill(self):
        try:
            text = core.load_skill()
        except Exception as error:
            self.error("No se pudieron abrir las instrucciones", error)
            return
        self.skill.setPlainText(text)
        self.skill_saved = self.skill.toPlainText()
        self.show_page(5)

    def save_skill(self, notify=True):
        self.skill_timer.stop()
        try:
            core.save_skill(self.skill.toPlainText())
        except Exception as error:
            self.status.setText(f"Instrucciones sin guardar: {error}")
            if notify:
                self.error("No se pudieron guardar las instrucciones", error)
            return False
        self.skill_saved = self.skill.toPlainText()
        self.status.setText("Instrucciones guardadas automáticamente.")
        return True

    def restore_skill(self):
        if not self.confirm(
            "Restaurar plantilla", "¿Reemplazar el texto del editor por la plantilla original?"
        ):
            return
        try:
            self.skill.setPlainText(core.load_default_skill())
        except Exception as error:
            self.error("No se pudo cargar la plantilla", error)

    def confirm_skill_changes(self):
        if self.skill.toPlainText() == self.skill_saved:
            return True
        return self.save_skill()

    def open_repository(self):
        QDesktopServices.openUrl(QUrl("https://github.com/dertin/backlogtalk"))

    def open_issue_tracker(self):
        QDesktopServices.openUrl(QUrl("https://github.com/dertin/backlogtalk/issues/"))

    def show_about(self):
        dialog = QDialog(self, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setObjectName("aboutPanel")
        dialog.setWindowTitle("Acerca de BacklogTalk")
        dialog.setMinimumWidth(540)
        dialog.setStyleSheet(
            "QDialog#aboutPanel { background: #202a35; border: 1px solid #526477; border-radius: 8px; }"
        )
        box = QVBoxLayout(dialog)
        box.setContentsMargins(24, 22, 24, 22)
        box.setSpacing(12)
        box.addWidget(label("Acerca de BacklogTalk", "sectionTitle"))
        box.addWidget(label(f"Versión {VERSION}", "muted"))
        box.addWidget(label("Autor: Guillermo Céspedes Tabárez"))
        box.addWidget(label("Licencia MIT", "muted"))
        box.addWidget(
            label(
                "¿Encontraste un problema? Podés reportarlo en GitHub. Antes de compartir un diagnóstico, revisá que no incluya datos privados.",
                "muted",
            )
        )
        row = QHBoxLayout()
        row.addWidget(button("Ver en GitHub", self.open_repository))
        row.addWidget(button("Reportar un error", self.open_issue_tracker))
        row.addStretch()
        row.addWidget(button("Cerrar", dialog.accept, "primary"))
        box.addLayout(row)
        self.show_notice(dialog, autoclose=False)
        return dialog

    def setup_tray(self):
        self.tray_idle_icon = tray_icon()
        self.tray_recording_icon = recording_tray_icon()
        self.tray = QSystemTrayIcon(self.tray_idle_icon, self)
        self.tray.setToolTip("BacklogTalk")
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction("Abrir BacklogTalk", self.show_main)
        self.tray_record = self.tray_menu.addAction(
            "Grabar nota", lambda: self.stop_record() if self.recording else self.shortcut_capture()
        )
        self.tray_meeting = self.tray_menu.addAction(
            "Grabar micrófono + salida", lambda: self.shortcut_capture("system")
        )
        self.tray_menu.addAction("Importar audio o video…", self.tray_import)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Tickets", lambda: self.tray_open_page(1))
        self.tray_menu.addAction("Historial", lambda: self.tray_open_page(2))
        self.tray_menu.addAction("Configuración", lambda: self.tray_open_page(3))
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Ver en GitHub", self.open_repository)
        self.tray_menu.addAction("Reportar un error", self.open_issue_tracker)
        self.tray_menu.addAction("Acerca de BacklogTalk", self.show_about)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Salir", self.request_exit)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(
            lambda reason: (
                self.show_main()
                if reason
                in (
                    QSystemTrayIcon.ActivationReason.Trigger,
                    QSystemTrayIcon.ActivationReason.DoubleClick,
                )
                else None
            )
        )
        self.tray.show()
        if self.recording:
            self.set_tray_recording(True)

    def set_tray_recording(self, active):
        if not self.tray:
            return
        self.tray_record.setText("Detener y guardar" if active else "Grabar nota")
        self.tray_meeting.setEnabled(not active)
        self.tray.setToolTip(
            (
                "BacklogTalk / Grabando micrófono y audio del equipo"
                if self.recording_source == "system"
                else "BacklogTalk / Grabando micrófono"
            )
            if active
            else "BacklogTalk"
        )
        self.tray_blink_timer.stop()
        self.tray_blink_on = active
        self.tray.setIcon(self.tray_recording_icon if active else self.tray_idle_icon)
        if active:
            self.tray_blink_timer.start()

    def blink_tray(self):
        if not self.tray or not self.recording:
            self.set_tray_recording(False)
            return
        self.tray_blink_on = not self.tray_blink_on
        self.tray.setIcon(self.tray_recording_icon if self.tray_blink_on else self.tray_idle_icon)

    def tray_available(self):
        return self.tray is not None and QSystemTrayIcon.isSystemTrayAvailable()

    def tray_open_page(self, page):
        self.show_main()
        self.show_page(page)

    def tray_import(self):
        self.show_main()
        self.show_page(0)
        self.choose_media()

    def request_exit(self):
        self.quit_requested = True
        if not self.isVisible():
            self.show_main()
        self.close()
        if not self.closing:
            self.quit_requested = False

    def closeEvent(self, event):
        if self.settings_dirty and not self.save_settings():
            event.ignore()
            return
        if self.tray_available() and not self.quit_requested:
            if self.pages.currentIndex() == 5 and not self.confirm_skill_changes():
                event.ignore()
                return
            if self.pages.currentIndex() == 4 and not self.save_review():
                event.ignore()
                return
            if self.note.toPlainText().strip() and self.save_note() is None:
                event.ignore()
                return
            if self.recording:
                self.popup.recording_started()
            self.hide()
            event.ignore()
            return
        if self.play_cancel is not None:
            self.play_cancel.set()
            self.playing = False
            self.close_after_playback = True
            self.status.setText("Deteniendo audio para salir…")
            event.ignore()
            return
        if self.busy or self.pending or self.recording_stopping:
            dialog = self.dialog(
                "Operación en curso",
                "Hay trabajo en curso. El texto y audio sin guardar pueden perderse. Si se está enviando un ticket, revisá Jira antes de reintentarlo.",
                QMessageBox.Icon.Warning,
            )
            dialog.addButton("Seguir esperando", QMessageBox.ButtonRole.RejectRole)
            force = dialog.addButton("Forzar cierre", QMessageBox.ButtonRole.DestructiveRole)
            dialog.exec()
            if dialog.clickedButton() == force:
                os._exit(0)
            event.ignore()
            return
        if self.pages.currentIndex() == 5 and not self.confirm_skill_changes():
            event.ignore()
            return
        if self.pages.currentIndex() == 4 and not self.save_review():
            event.ignore()
            return
        if self.recording:
            if not self.confirm(
                "Grabación en curso", "¿Detener y guardar el audio antes de salir?"
            ):
                event.ignore()
                return
            self.close_after_recording = True
            self.stop_record()
            event.ignore()
            return
        if self.note.toPlainText().strip() and self.save_note() is None:
            event.ignore()
            return
        self.closing = True
        self.jobs.put((2, next(self.order), None, None, None))
        self.popup.hide()
        event.accept()
        if self.tray:
            self.tray.hide()
        QTimer.singleShot(0, QApplication.instance().quit)


def main():
    instance = Instance(core.DEFAULT_CONFIG_DIR)
    try:
        command = (
            "capture-system"
            if "--capture-system" in sys.argv
            else "capture"
            if "--capture" in sys.argv
            else "open-history"
            if "--open-history" in sys.argv
            else "open"
        )
        if not instance.acquire_or_send(command):
            return
        core.db_connect().close()
        application = QApplication(sys.argv)
        application.setStyleSheet(STYLE)
        application.setWindowIcon(QIcon(str(core.ROOT / "assets/backlogtalk-mark.svg")))
        application.setQuitOnLastWindowClosed(False)
        window = VoiceWindow(instance)
        window.setup_tray()
        if command in ("capture", "capture-system"):
            window.capture_only = True
            QTimer.singleShot(
                600,
                lambda: window.shortcut_capture("system" if command == "capture-system" else "mic"),
            )
        else:
            window.show()
            if command == "open-history":
                window.show_page(2)
        application.exec()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
