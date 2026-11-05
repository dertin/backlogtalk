import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from backlogtalk import core, qt_app


class QtInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setStyleSheet(qt_app.STYLE)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        shortcuts_guard = patch.object(
            qt_app.shortcuts, "gnome_global_shortcuts", return_value=None
        )
        shortcuts_guard.start()
        self.addCleanup(shortcuts_guard.stop)
        root = Path(self.tmp.name)
        for name, value in [
            ("APP_HOME", root),
            ("AUDIO_DIR", root / "audio"),
            ("DB_FILE", root / "db.sqlite3"),
            ("CONFIG_DIR", root / "config"),
            ("CONFIG_FILE", root / "config/config.json"),
        ]:
            p = patch.object(core, name, value)
            p.start()
            self.addCleanup(p.stop)
        core.save_config(
            core.DEFAULT_CONFIG
            | {
                "projects": ["DEMO"],
                "last_project": "DEMO",
                "jira_site": "example.atlassian.net",
                "favorite_epics": {"DEMO": ["DEMO-414", "DEMO-2252", "DEMO-1245"]},
            }
        )
        core.db_connect().close()
        transcription_guard = patch.object(
            core, "transcribe", side_effect=RuntimeError("La prueba debe simular la transcripción")
        )
        transcription_guard.start()
        self.addCleanup(transcription_guard.stop)
        self.window = qt_app.VoiceWindow()
        self.window.show()
        self.application.processEvents()
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        self.window.timer.stop()
        if not self.window.closing:
            self.window.closing = True
            self.window.jobs.put((-1, next(self.window.order), None, None, None))
            self.window.hide()
        self.window.transcription_thread.join(timeout=5)
        self.window.popup.hide()
        self.window.popup.deleteLater()
        self.window.deleteLater()
        self.application.processEvents()

    def click_row(self, widget, row, modifier=Qt.KeyboardModifier.NoModifier):
        self.application.processEvents()
        item = widget.item(row)
        position = widget.visualItemRect(item).center()
        QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, modifier, position)
        self.application.processEvents()

    def wait_recording_stop(self):
        deadline = time.monotonic() + 5
        while self.window.recording_stopping and time.monotonic() < deadline:
            self.window.poll()
            self.application.processEvents()
            QTest.qWait(20)
        self.assertFalse(self.window.recording_stopping)

    def test_progress_does_not_repeat_status_message(self):
        self.window.busy = True
        self.window.status.setText("Consultando modelos de Cursor…")
        self.window.update_progress()
        self.assertEqual(self.window.progress_label.text(), "")
        self.assertEqual(self.window.status.text(), "Consultando modelos de Cursor…")
        self.window.busy = False
        self.window.update_progress()

    def test_system_and_daily_metrics_remain_visible_when_idle(self):
        from types import SimpleNamespace

        system = SimpleNamespace(
            cpu_percent=lambda _: 25.0,
            virtual_memory=lambda: SimpleNamespace(
                percent=50.0, used=4 * 1024**3, total=8 * 1024**3
            ),
        )
        self.window.resource_system = system
        self.window.resource_last_sample = 0
        self.window.update_resources()
        with patch.object(core, "daily_activity", return_value=(2, 100)):
            self.window.backend.setCurrentText("ollama")
            self.window.update_daily_activity()
        self.assertFalse(self.window.progress_panel.isVisible())
        self.assertTrue(self.window.system_resource_label.isVisible())
        self.assertEqual(self.window.system_resource_label.text(), "CPU 25% | RAM 50%")
        self.assertEqual(self.window.daily_activity_label.text(), "Jira hoy 2 | Ollama 100 tok")
        self.window.backend.setCurrentText("cursor")
        with patch.object(core, "daily_activity", return_value=(2, 100)):
            self.window.update_daily_activity()
        self.assertEqual(self.window.daily_activity_label.text(), "Jira hoy 2 | Cursor tok --")

    def test_window_has_real_transparent_antialiased_corners(self):
        self.assertTrue(self.window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        image = self.window.grab().toImage()
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
        self.assertTrue(
            any(0 < image.pixelColor(x, y).alpha() < 255 for x in range(24) for y in range(24))
        )

    def test_mouse_wheel_does_not_change_closed_selectors(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent

        for combo in (
            self.window.source,
            self.window.whisper_model,
            self.window.backend,
            self.window.mics,
        ):
            old = combo.currentIndex()
            event = QWheelEvent(
                QPointF(5, 5),
                QPointF(5, 5),
                QPoint(),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            self.application.sendEvent(combo, event)
            self.assertEqual(combo.currentIndex(), old)

    def test_shift_ctrl_and_delete_tickets_preserve_notes_and_jira(self):
        capture = core.add_capture("DEMO", None, "Nota original")
        core.save_drafts(
            capture,
            [
                {"title": f"Tarea ejemplo {i}", "description": "Descripción", "type": "Task"}
                for i in range(4)
            ],
        )
        self.window.refresh()
        self.window.show_page(1)
        self.application.processEvents()
        self.click_row(self.window.tickets, 0, Qt.KeyboardModifier.ControlModifier)
        self.click_row(self.window.tickets, 2, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(self.window.selected_ids(self.window.tickets)), 2)
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.window.refresh()
        self.assertEqual(len(self.window.selected_ids(self.window.tickets)), 2)
        with (
            patch.object(self.window, "confirm", return_value=True),
            patch.object(core.subprocess, "run") as remote,
        ):
            self.window.tickets.setFocus()
            QTest.keyClick(self.window.tickets, Qt.Key.Key_Delete)
            self.application.processEvents()
            remote.assert_not_called()
        self.assertEqual(len(core.list_tickets()), 2)
        self.assertEqual(core.list_captures()[0]["transcript"], "Nota original")

    def test_shift_range_and_delete_notes_preserve_linked_ticket(self):
        ids = [core.add_capture("DEMO", None, f"Nota {i}") for i in range(4)]
        core.save_drafts(
            ids[-1],
            [{"title": "Una tarea de ejemplo", "description": "Descripción", "type": "Task"}],
        )
        self.window.refresh()
        self.window.show_page(2)
        self.application.processEvents()
        self.click_row(self.window.history, 0)
        self.click_row(self.window.history, 2, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(len(self.window.selected_ids(self.window.history)), 3)
        with patch.object(self.window, "confirm", return_value=True):
            self.window.history.setFocus()
            QTest.keyClick(self.window.history, Qt.Key.Key_Delete)
        self.assertEqual(len(core.list_captures()), 1)
        self.assertEqual(len(core.list_tickets()), 1)

    def test_review_loads_and_close_does_not_publish(self):
        capture = core.add_capture("DEMO", "DEMO-414", "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket de prueba", "description": "Descripción", "type": "Task"}]
        )
        self.window.refresh()
        self.window.show_page(1)
        self.application.processEvents()
        self.click_row(self.window.tickets, 0)
        self.assertEqual(self.window.pages.currentIndex(), 1)
        position = self.window.tickets.visualItemRect(self.window.tickets.item(0)).center()
        QTest.mouseDClick(
            self.window.tickets.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            position,
        )
        self.application.processEvents()
        self.assertEqual(self.window.pages.currentIndex(), 4)
        self.assertEqual(self.window.review_title.text(), "Ticket de prueba")
        self.assertEqual(self.window.review_body.toPlainText(), "Descripción")
        with (
            patch.object(self.window, "background") as background,
            patch.object(core, "publish_ticket") as publish,
        ):
            self.window.publish()
            background.assert_called_once()
            publish.assert_not_called()

    def test_review_selects_project_epics_and_preserves_existing_one(self):
        self.window.config["favorite_epics_by_site"] = {
            "example.atlassian.net": {
                "projects": {"DEMO": ["DEMO-2"]},
                "names": {"DEMO-2": "Favorita"},
            }
        }
        capture = core.add_capture("DEMO", "DEMO-1", "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()
        self.assertEqual(self.window.review_epic.currentData(), "DEMO-1")
        self.assertGreaterEqual(self.window.review_epic.findText("Favorita / DEMO-2"), 0)
        self.window.review_epic.setCurrentIndex(self.window.review_epic.findData("DEMO-2"))
        self.assertTrue(self.window.save_review())
        self.assertEqual(core.list_tickets()[0]["epic_key"], "DEMO-2")

    def test_synced_fields_persist_in_draft_across_type_changes(self):
        import json

        fields = [
            {
                "id": "customfield_101",
                "name": "Puntos",
                "required": True,
                "schema": {"type": "number"},
            },
            {
                "id": "customfield_102",
                "name": "Clasificación",
                "schema": {"type": "option"},
                "allowedValues": [{"id": "2", "value": "Alta"}, {"id": "3", "value": "Baja"}],
            },
        ]
        with core.db_connect() as db:
            for kind in ("Tarea", "Error"):
                db.execute(
                    "INSERT INTO jira_field_cache VALUES (?,?,?,?,?)",
                    ("example.atlassian.net", "DEMO", kind, json.dumps(fields), "ahora"),
                )
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()
        editors = self.window.review_field_editors
        editors["customfield_101"][0].setText("3")
        editors["customfield_102"][0].setCurrentIndex(1)
        self.window.review_type.setCurrentText("Error")
        self.window.review_type.setCurrentText("Tarea")
        self.assertEqual(self.window.review_field_editors["customfield_101"][0].text(), "3.0")
        self.assertEqual(
            self.window.review_field_editors["customfield_102"][0].currentData(), {"id": "2"}
        )
        self.assertTrue(self.window.save_review())
        saved = core.ticket_extra_fields(self.window.review_id, "example.atlassian.net", "Task")
        self.assertEqual(saved["customfield_101"], 3)
        self.assertEqual(saved["customfield_102"], {"id": "2"})

    def test_jira_fields_popup_keeps_invalid_edits_open_and_saves(self):
        import json

        fields = [{"id": "customfield_101", "name": "Puntos", "schema": {"type": "number"}}]
        with core.db_connect() as db:
            db.execute(
                "INSERT INTO jira_field_cache VALUES (?,?,?,?,?)",
                ("example.atlassian.net", "DEMO", "Tarea", json.dumps(fields), "ahora"),
            )
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()
        seen = []

        def interact():
            dialog = self.window.review_fields_dialog
            editor = self.window.review_field_editors["customfield_101"][0]
            seen.append((dialog.isVisible(), editor.isVisible(), dialog.height()))
            editor.setText("inválido")
            dialog.accept()
            seen.append((dialog.isVisible(), self.window.review_fields_error.isVisible()))
            editor.setText("5")
            dialog.accept()

        QTimer.singleShot(0, interact)
        self.window.show_review_fields()
        self.assertTrue(seen[0][0])
        self.assertTrue(seen[0][1])
        self.assertGreaterEqual(seen[0][2], 400)
        self.assertEqual(seen[1], (True, True))
        saved = core.ticket_extra_fields(self.window.review_id, "example.atlassian.net", "Task")
        self.assertEqual(saved["customfield_101"], 5)

    def test_team_and_sprint_use_selectors_and_save_native_values(self):
        import json

        from PySide6.QtWidgets import QComboBox

        team_id = "0454b33c-a128-4720-b0ee-47ad7b7e8983"
        fields = [
            {
                "id": "customfield_1",
                "name": "Team",
                "schema": {"type": "team"},
                "backlogtalk_options": [{"label": "Equipo", "value": team_id}],
            },
            {
                "id": "customfield_2",
                "name": "Sprint",
                "schema": {"type": "array", "custom": "com.pyxis.greenhopper.jira:gh-sprint"},
                "backlogtalk_options": [{"label": "Sprint 8 (activo)", "value": 123}],
            },
        ]
        with core.db_connect() as db:
            db.execute(
                "INSERT INTO jira_field_cache VALUES (?,?,?,?,?)",
                ("example.atlassian.net", "DEMO", "Tarea", json.dumps(fields), "ahora"),
            )
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()
        team = self.window.review_field_editors["customfield_1"][0]
        sprint = self.window.review_field_editors["customfield_2"][0]
        self.assertIsInstance(team, QComboBox)
        self.assertIsInstance(sprint, QComboBox)
        team.setCurrentIndex(1)
        sprint.setCurrentIndex(1)
        self.assertTrue(self.window.save_review())
        saved = core.ticket_extra_fields(self.window.review_id, "example.atlassian.net", "Task")
        self.assertEqual(saved["customfield_1"], team_id)
        self.assertEqual(saved["customfield_2"], 123)

    def test_jira_impediment_is_a_checkbox_and_preserves_option_id(self):
        import json

        from PySide6.QtWidgets import QCheckBox

        field = {
            "id": "customfield_10021",
            "name": "Flagged",
            "schema": {"type": "array"},
            "allowedValues": [{"id": "10019", "value": "Impediment"}],
        }
        with core.db_connect() as db:
            db.execute(
                "INSERT INTO jira_field_cache VALUES (?,?,?,?,?)",
                ("example.atlassian.net", "DEMO", "Tarea", json.dumps([field]), "ahora"),
            )
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()
        editor, mode = self.window.review_field_editors["customfield_10021"]
        self.assertIsInstance(editor, QCheckBox)
        self.assertEqual(mode, "flag")
        self.assertFalse(editor.isChecked())
        editor.setChecked(True)
        self.assertTrue(self.window.save_review())
        self.assertEqual(
            core.ticket_extra_fields(self.window.review_id, "example.atlassian.net", "Task")[
                "customfield_10021"
            ],
            [{"id": "10019"}],
        )
        editor.setChecked(False)
        self.assertTrue(self.window.save_review())
        self.assertEqual(
            core.ticket_extra_fields(self.window.review_id, "example.atlassian.net", "Task")[
                "customfield_10021"
            ],
            [],
        )

    def test_date_picker_and_priority_selector_preserve_values(self):
        import json

        from PySide6.QtCore import QDate
        from PySide6.QtWidgets import QComboBox

        fields = [{"id": "customfield_3", "name": "Fecha de inicio", "schema": {"type": "date"}}]
        priorities = [{"label": "Medium", "value": "3"}, {"label": "Highest", "value": "1"}]
        with core.db_connect() as db:
            db.execute(
                "INSERT INTO jira_field_cache VALUES (?,?,?,?,?)",
                ("example.atlassian.net", "DEMO", "Tarea", json.dumps(fields), "ahora"),
            )
            db.execute(
                "INSERT INTO jira_priority_cache VALUES (?,?,?)",
                ("example.atlassian.net", json.dumps(priorities), "ahora"),
            )
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        ticket_id = core.list_tickets()[0]["id"]
        core.save_ticket_extra_fields(
            ticket_id, "example.atlassian.net", "Task", {"standard:priority": "Medium"}
        )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()
        priority = self.window.review_field_editors["standard:priority"][0]
        date = self.window.review_field_editors["customfield_3"][0]
        self.assertIsInstance(priority, QComboBox)
        self.assertEqual(priority.currentText(), "Medium")
        self.assertTrue(date.date_picker.calendarPopup())
        self.assertFalse(date.date_enabled.isChecked())
        self.assertTrue(self.window.save_review())
        self.assertIsNone(
            core.ticket_extra_fields(ticket_id, "example.atlassian.net", "Task")["customfield_3"]
        )
        date.date_enabled.setChecked(True)
        date.date_picker.setDate(QDate(2026, 12, 9))
        priority.setCurrentIndex(priority.findText("Highest"))
        self.assertTrue(self.window.save_review())
        values = core.ticket_extra_fields(ticket_id, "example.atlassian.net", "Task")
        self.assertEqual(values["customfield_3"], "2026-12-09")
        self.assertEqual(values["standard:priority"], "1")
        self.window.open_ticket()
        self.assertEqual(
            self.window.review_field_editors["customfield_3"][0].date_picker.date(),
            QDate(2026, 12, 9),
        )

    def test_preparing_draft_uses_cached_fields_transcript_and_saves_proposal(self):
        import json

        fields = [{"id": "customfield_101", "name": "Puntos", "schema": {"type": "number"}}]
        with core.db_connect() as db:
            db.execute(
                "INSERT INTO jira_field_cache VALUES (?,?,?,?,?)",
                ("example.atlassian.net", "DEMO", "Tarea", json.dumps(fields), "ahora"),
            )
        self.window.note.setPlainText("Asignar tres puntos a la tarea")
        draft = [{"title": "Ticket ejemplo", "description": "Texto", "type": "Tarea"}]
        proposal = '{"fields":{"customfield_101":3},"origins":{"customfield_101":"transcripción"}}'
        with (
            patch.object(core, "draft_with_cursor", return_value=draft),
            patch.object(core, "plan_jira_fields", return_value=proposal) as plan,
            patch.object(self.window, "background") as background,
        ):
            self.window.prepare()
            worker, done = background.call_args.args[:2]
            done(worker())
        self.assertEqual(plan.call_args.args[-1], "Asignar tres puntos a la tarea")
        ticket = core.list_tickets()[0]
        self.assertEqual(ticket["ticket_type"], "Task")
        self.assertEqual(
            core.ticket_extra_fields(ticket["id"], "example.atlassian.net", "Task"),
            {"customfield_101": 3},
        )
        self.assertEqual(
            core.ticket_extra_origins(ticket["id"], "example.atlassian.net", "Task"),
            {"customfield_101": "transcripción"},
        )

    def test_open_published_ticket_uses_saved_site(self):
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        ticket = core.list_tickets()[0]
        with core.db_connect() as db:
            db.execute(
                "UPDATE tickets SET state='published', jira_key='DEMO-42', jira_site='original.atlassian.net' WHERE id=?",
                (ticket["id"],),
            )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.assertTrue(self.window.open_jira_button.isEnabled())
        with patch.object(qt_app.QDesktopServices, "openUrl", return_value=True) as open_url:
            self.window.open_selected_jira_ticket()
        self.assertEqual(
            open_url.call_args.args[0].toString(), "https://original.atlassian.net/browse/DEMO-42"
        )

    def test_created_ticket_notice_opens_jira_and_expires(self):
        from PySide6.QtWidgets import QPushButton

        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket ejemplo", "description": "Texto", "type": "Task"}]
        )
        self.window.refresh()
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()
        with patch.object(self.window, "background") as background:
            self.window.publish()
        done = background.call_args.args[1]
        done(("DEMO-42", ""))
        dialog = next(iter(self.window.notice_windows))
        self.assertFalse(dialog.isModal())
        self.assertGreaterEqual(dialog.findChild(QTimer, "noticeTimeout").interval(), 15_000)
        self.assertLessEqual(dialog.findChild(QTimer, "noticeTimeout").interval(), 45_000)
        self.assertFalse(dialog.isWindow())
        open_button = next(
            button
            for button in dialog.findChildren(QPushButton)
            if button.text() == "Abrir en Jira"
        )
        with patch.object(qt_app.QDesktopServices, "openUrl", return_value=True) as open_url:
            open_button.click()
        self.assertEqual(
            open_url.call_args.args[0].toString(), "https://example.atlassian.net/browse/DEMO-42"
        )
        self.assertFalse(dialog.isVisible())

        notice = self.window.info("Listo", "Operación terminada")
        notice.findChild(QTimer, "noticeTimeout").timeout.emit()
        self.assertFalse(notice.isVisible())
        self.assertNotIn(notice, self.window.notice_windows)

    def test_notice_does_not_block_recording_shortcut(self):
        notice = self.window.info("Listo", "Operación terminada")
        notice.activateWindow()
        self.application.processEvents()
        with patch.object(self.window, "start_record") as record:
            QTest.keyClick(
                notice,
                Qt.Key.Key_J,
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
            )
            self.application.processEvents()
        record.assert_called_once_with(quick=True, source="mic")
        self.assertTrue(notice.isVisible())

    def test_notice_is_readable_child_and_auto_closes(self):
        from PySide6.QtWidgets import QLabel

        notice = self.window.info(
            "Grabación en curso", "Detené la grabación antes de cambiar de pantalla."
        )
        self.application.processEvents()
        self.assertFalse(notice.isWindow())
        self.assertGreaterEqual(notice.width(), 430)
        for item in notice.findChildren(QLabel):
            self.assertGreaterEqual(item.height(), item.heightForWidth(item.width()))
        self.assertEqual(notice.pos().x(), (self.window.width() - notice.width()) // 2)
        notice.findChild(QTimer, "noticeTimeout").timeout.emit()
        self.assertFalse(notice.isVisible())

    def test_confirmation_remains_modal_without_timeout(self):
        seen = []

        def inspect_confirmation():
            dialog = self.application.activeModalWidget()
            self.assertIsNotNone(dialog)
            self.assertIsNone(dialog.findChild(QTimer, "noticeTimeout"))
            seen.append(True)
            next(button for button in dialog.buttons() if button.text() == "Cancelar").click()

        QTimer.singleShot(0, inspect_confirmation)
        self.assertFalse(self.window.confirm("Confirmar", "¿Continuar?"))
        self.assertEqual(seen, [True])

    def test_rejecting_proposed_jira_values_cancels_without_error(self):
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture, [{"title": "Ticket de ejemplo", "description": "Descripción", "type": "Task"}]
        )
        self.window.refresh()
        self.window.show_page(1)
        self.window.tickets.setCurrentRow(0)
        self.window.open_ticket()

        def publish(_ticket, _site, approve_plan):
            plan = {
                "site": "example.atlassian.net",
                "project": "DEMO",
                "epic": None,
                "type": "Task",
                "title": "Ticket",
                "fields": {},
                "standard": {"assignee": "me"},
                "comment": "",
            }
            if not approve_plan(plan):
                raise core.PublicationCancelled()
            raise AssertionError("No debe crear el ticket")

        with (
            patch.object(core, "publish_ticket", side_effect=publish),
            patch.object(self.window, "confirm_publish_plan", return_value=False),
            patch.object(self.window, "error") as error,
        ):
            self.window.publish()
            deadline = time.monotonic() + 5
            while self.window.busy and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)
            error.assert_not_called()
            self.assertFalse(self.window.busy)
        self.assertIn("Envío cancelado", self.window.status.text())

    def test_skill_editor_and_storage(self):
        self.window.open_skill()
        self.window.skill.setPlainText("Contexto y salida")
        self.assertTrue(self.window.save_skill())
        self.assertIn("Contexto y salida", core.build_draft_prompt("Nota"))

    def test_project_and_epic_context_is_saved(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QPushButton, QTextEdit

        errors = []

        def interact():
            dialog = self.application.activeModalWidget()
            try:
                self.assertIsInstance(dialog, QDialog)
                entries = dialog.findChildren(QLineEdit)
                entries[2].clear()
                next(b for b in dialog.findChildren(QPushButton) if b.text() == "Agregar").click()
                editor = dialog.findChild(QTextEdit)
                editor.setPlainText("Campos del proyecto")
                entries[2].setText("DEMO-414")
                next(b for b in dialog.findChildren(QPushButton) if b.text() == "Agregar").click()
                editor.setPlainText("Campos de la épica")
                listing = dialog.findChild(QListWidget)
                listing.setCurrentRow(0)
                listing.setCurrentRow(1)
                self.assertEqual(editor.toPlainText(), "Campos del proyecto")
                listing.setCurrentRow(2)
                self.assertEqual(editor.toPlainText(), "Campos de la épica")
                editor.setPlainText("Cambios finales de la épica")
            except Exception as error:
                errors.append(error)
            finally:
                next(
                    b for b in dialog.findChildren(QPushButton) if b.text() == "Guardar y cerrar"
                ).click()

        QTimer.singleShot(0, interact)
        self.window.edit_jira_context()
        if errors:
            raise errors[0]
        self.assertEqual(
            core.jira_instructions(core.load_config(), "example.atlassian.net", "DEMO", "DEMO-414"),
            ("Campos del proyecto", "Cambios finales de la épica"),
        )
        self.assertTrue(self.window.show_page(3))

    def test_open_model_cache_uses_huggingface_setting(self):
        with (
            patch("huggingface_hub.constants.HF_HUB_CACHE", self.tmp.name),
            patch.object(qt_app.QDesktopServices, "openUrl", return_value=True) as opened,
        ):
            self.window.open_model_cache()
        self.assertEqual(
            Path(opened.call_args.args[0].toLocalFile()).resolve(), Path(self.tmp.name).resolve()
        )

    def test_about_and_tray_expose_project_and_issue_links(self):
        from PySide6.QtWidgets import QLabel, QPushButton

        dialog = self.window.show_about()
        self.application.processEvents()
        texts = [item.text() for item in dialog.findChildren(QLabel)]
        self.assertTrue(any("Guillermo Céspedes Tabárez" in text for text in texts))
        self.assertFalse(any("colaboradores" in text.lower() for text in texts))
        self.assertTrue(any("MIT" in text for text in texts))
        self.assertFalse(dialog.isWindow())
        self.assertIsNone(dialog.findChild(QTimer, "noticeTimeout"))
        for item in dialog.findChildren(QLabel):
            self.assertGreaterEqual(item.height(), item.heightForWidth(item.width()))
        buttons = {item.text(): item for item in dialog.findChildren(QPushButton)}
        with patch.object(qt_app.QDesktopServices, "openUrl", return_value=True) as open_url:
            buttons["Ver en GitHub"].click()
            buttons["Reportar un error"].click()
        self.assertEqual(
            [call.args[0].toString() for call in open_url.call_args_list],
            [
                "https://github.com/dertin/backlogtalk",
                "https://github.com/dertin/backlogtalk/issues/",
            ],
        )
        dialog.close()
        self.window.setup_tray()
        try:
            actions = {action.text() for action in self.window.tray_menu.actions()}
            self.assertTrue(
                {"Ver en GitHub", "Reportar un error", "Acerca de BacklogTalk"} <= actions
            )
        finally:
            self.window.tray.hide()

    def test_delete_epic_instructions_keeps_other_scopes(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QListWidget, QPushButton

        config = core.load_config()
        config["jira_contexts"] = {
            "example.atlassian.net": {
                "DEMO": {"instructions": "Proyecto", "epics": {"DEMO-414": "A", "DEMO-1245": "B"}}
            }
        }
        core.save_config(config)
        self.window.config = config
        errors = []

        def interact():
            dialog = self.application.activeModalWidget()
            try:
                listing = dialog.findChild(QListWidget)
                index = next(
                    i for i in range(listing.count()) if "DEMO-414" in listing.item(i).text()
                )
                listing.setCurrentRow(index)
                next(
                    b
                    for b in dialog.findChildren(QPushButton)
                    if b.text() == "Eliminar instrucciones"
                ).click()
            except Exception as error:
                errors.append(error)
            finally:
                dialog.accept()

        with patch.object(self.window, "confirm", return_value=True):
            QTimer.singleShot(0, interact)
            self.window.edit_jira_context()
        if errors:
            raise errors[0]
        config = core.load_config()
        self.assertEqual(
            core.jira_instructions(config, "example.atlassian.net", "DEMO", "DEMO-414"),
            ("Proyecto", ""),
        )
        self.assertEqual(
            core.jira_instructions(config, "example.atlassian.net", "DEMO", "DEMO-1245"),
            ("Proyecto", "B"),
        )

    def test_recording_transcribes_in_background_and_saves(self):
        class Recorder:
            def start(self, device):
                pass

            def stop_to_file(self):
                path = core.AUDIO_DIR / "audio.wav"
                path.write_bytes(b"audio")
                return path

        self.window.recorder = Recorder()
        with patch.object(core, "transcribe", return_value="Texto reconocido"):
            self.window.start_record()
            self.assertTrue(self.window.recording)
            self.window.stop_record()
            deadline = time.monotonic() + 4
            while (
                self.window.pending or self.window.recording_stopping
            ) and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
        self.assertFalse(self.window.pending)
        self.assertEqual(core.list_captures()[0]["transcript"], "Texto reconocido")
        self.assertEqual(self.window.note.toPlainText(), "Texto reconocido")

    def test_stalled_recording_stop_does_not_block_ui(self):
        import threading

        entered = threading.Event()
        release = threading.Event()

        class Recorder:
            level = 0

            def start(self, _device):
                pass

            def stop_to_file(self):
                entered.set()
                release.wait(5)
                path = core.AUDIO_DIR / "stalled.wav"
                path.write_bytes(b"audio")
                return path

        self.window.recorder = Recorder()
        with patch.object(core, "transcribe", return_value="Texto"):
            try:
                self.window.shortcut_capture()
                self.window.shortcut_capture()
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.window.recording_stopping)
                self.application.processEvents()
                self.assertIn("Guardando", self.window.status.text())
            finally:
                release.set()
            deadline = time.monotonic() + 5
            while (
                self.window.recording_stopping or self.window.pending
            ) and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)
            self.assertFalse(self.window.recording_stopping)
            self.assertFalse(self.window.recording)

    def test_close_during_recording_finishes_after_saving(self):
        class Recorder:
            level = 0

            def start(self, _device):
                pass

            def stop_to_file(self):
                path = core.AUDIO_DIR / "close.wav"
                path.write_bytes(b"audio")
                return path

        self.window.recorder = Recorder()
        with (
            patch.object(core, "transcribe", return_value="Texto"),
            patch.object(self.window, "confirm", return_value=True),
            patch.object(self.window, "tray_available", return_value=False),
            patch("backlogtalk.qt_app.QTimer.singleShot"),
        ):
            self.window.start_record()
            self.window.close()
            deadline = time.monotonic() + 5
            while (
                self.window.recording_stopping or self.window.pending
            ) and time.monotonic() < deadline:
                self.window.poll()
                self.application.processEvents()
                QTest.qWait(20)
            self.assertTrue(self.window.closing)
            self.assertFalse(self.window.isVisible())
        self.assertEqual(core.list_captures()[0]["transcript"], "Texto")

    def test_close_during_recording_keeps_overflow_warning_visible(self):
        class Recorder:
            level = 0
            overflowed = True

            def start(self, _device):
                pass

            def stop_to_file(self):
                path = core.AUDIO_DIR / "overflow.wav"
                path.write_bytes(b"audio")
                return path

        self.window.recorder = Recorder()
        with (
            patch.object(core, "transcribe", return_value="Texto"),
            patch.object(self.window, "confirm", return_value=True),
            patch.object(self.window, "tray_available", return_value=False),
        ):
            self.window.start_record()
            self.window.close()
            deadline = time.monotonic() + 5
            while (
                self.window.recording_stopping or self.window.pending
            ) and time.monotonic() < deadline:
                self.window.poll()
                self.application.processEvents()
                QTest.qWait(20)
        self.assertFalse(self.window.closing)
        self.assertTrue(self.window.failure_windows)
        for dialog in list(self.window.failure_windows):
            dialog.close()

    def test_tray_blinks_while_recording_and_resets_when_stopped(self):
        class Recorder:
            level = 0

            def start(self, device):
                pass

            def stop_to_file(self):
                path = core.AUDIO_DIR / "tray.wav"
                path.write_bytes(b"audio")
                return path

        self.window.setup_tray()
        self.window.recorder = Recorder()
        try:
            with patch.object(core, "transcribe", return_value="Texto"):
                self.window.start_record()
                self.assertTrue(self.window.tray_blink_timer.isActive())
                self.assertIn("Grabando", self.window.tray.toolTip())
                red = self.window.tray.icon().pixmap(64, 64).toImage().pixelColor(51, 51)
                self.assertGreater(red.red(), 200)
                self.assertLess(red.green(), 100)
                for size in (16, 24, 32):
                    pixel = (
                        self.window.tray.icon()
                        .pixmap(size, size)
                        .toImage()
                        .pixelColor(int(size * 0.8), int(size * 0.8))
                    )
                    self.assertGreater(pixel.red(), 180)
                    self.assertLess(pixel.green(), 120)
                QTest.qWait(750)
                self.assertFalse(self.window.tray_blink_on)
                idle = self.window.tray.icon().pixmap(64, 64).toImage().pixelColor(51, 51)
                self.assertNotEqual(red, idle)
                self.window.blink_tray()
                self.assertTrue(self.window.tray_blink_on)
                self.window.stop_record()
                self.wait_recording_stop()
                self.assertFalse(self.window.tray_blink_timer.isActive())
                self.assertEqual(self.window.tray.toolTip(), "BacklogTalk")
                self.assertEqual(self.window.tray_record.text(), "Grabar nota")
                deadline = time.monotonic() + 4
                while self.window.pending and time.monotonic() < deadline:
                    self.application.processEvents()
                    QTest.qWait(25)
        finally:
            self.window.tray.hide()

    def test_cancel_restores_tray_icon_and_stops_blinking(self):
        class Recorder:
            level = 0

            def start(self, device):
                pass

            def discard(self):
                pass

        self.window.setup_tray()
        self.window.recorder = Recorder()
        try:
            self.window.start_record(quick=True)
            self.assertTrue(self.window.tray_blink_timer.isActive())
            self.assertTrue(self.window.tray_blink_on)
            with patch.object(self.window, "confirm", return_value=True):
                self.window.cancel_record()
            self.wait_recording_stop()
            self.assertFalse(self.window.recording)
            self.assertFalse(self.window.tray_blink_timer.isActive())
            self.assertFalse(self.window.tray_blink_on)
            self.assertEqual(self.window.tray.toolTip(), "BacklogTalk")
            for size in (16, 22, 32, 64):
                actual = self.window.tray.icon().pixmap(size, size).toImage()
                idle = self.window.tray_idle_icon.pixmap(size, size).toImage()
                self.assertEqual(actual, idle)
            QTest.qWait(750)
            self.assertFalse(self.window.tray_blink_on)
            self.assertEqual(core.list_captures(), [])
        finally:
            self.window.tray.hide()

    def test_tray_exposes_sources_and_navigation(self):
        self.window.setup_tray()
        try:
            actions = {action.text(): action for action in self.window.tray_menu.actions()}
            for name in (
                "Abrir BacklogTalk",
                "Grabar nota",
                "Grabar micrófono + salida",
                "Importar audio o video…",
                "Tickets",
                "Historial",
                "Configuración",
                "Salir",
            ):
                self.assertIn(name, actions)
            with patch.object(self.window, "shortcut_capture") as capture:
                actions["Grabar micrófono + salida"].trigger()
                capture.assert_called_once_with("system")
            with patch.object(self.window, "show_page") as page:
                actions["Configuración"].trigger()
                page.assert_called_once_with(3)
        finally:
            self.window.tray.hide()

    def test_tray_keeps_recording_indicator_if_microphone_failed_to_stop(self):
        class Stream:
            active = True

        class Recorder:
            level = 0
            stream = Stream()

            def start(self, device):
                pass

            def stop_to_file(self):
                raise RuntimeError("No se pudo detener")

        self.window.setup_tray()
        self.window.recorder = Recorder()
        try:
            self.window.start_record()
            with patch.object(self.window, "show_media_failure") as error:
                self.window.stop_record()
                self.wait_recording_stop()
                error.assert_called_once()
            self.assertTrue(self.window.recording)
            self.assertTrue(self.window.tray_blink_timer.isActive())
            self.assertIn("Grabando", self.window.tray.toolTip())
        finally:
            self.window.recording = False
            self.window.set_tray_recording(False)
            self.window.tray.hide()

    def test_tray_resets_if_audio_save_fails_after_microphone_stops(self):
        class Recorder:
            level = 0
            stream = None

            def start(self, device):
                pass

            def stop_to_file(self):
                raise RuntimeError("No se recibió audio")

        self.window.setup_tray()
        self.window.recorder = Recorder()
        try:
            self.window.start_record()
            with patch.object(self.window, "show_media_failure") as error:
                self.window.stop_record()
                self.wait_recording_stop()
                error.assert_called_once()
            self.assertFalse(self.window.recording)
            self.assertFalse(self.window.tray_blink_timer.isActive())
            self.assertEqual(self.window.tray.toolTip(), "BacklogTalk")
        finally:
            self.window.tray.hide()

    def fake_meeting_recorder(self):
        import wave

        class Recorder:
            level = 0
            active = False

            def __init__(self):
                self.devices = None
                self.stopped = False

            def start(self, microphone, output):
                self.devices = (microphone, output)
                self.active = True

            def stop_sources(self):
                self.stopped = True
                self.active = False
                paths = []
                for name in ("mic-test.wav", "output-test.wav"):
                    path = core.AUDIO_DIR / name
                    with wave.open(str(path), "wb") as out:
                        out.setnchannels(1)
                        out.setsampwidth(2)
                        out.setframerate(16000)
                        out.writeframes(b"\0\0" * 16000)
                    paths.append(path)
                return (*paths, 0, 0)

        return Recorder()

    def test_system_recording_uses_tray_indicator_and_names_source(self):
        rec = self.fake_meeting_recorder()
        self.window.setup_tray()
        try:
            with (
                patch("backlogtalk.qt_app.system_output_device", return_value=12),
                patch("backlogtalk.qt_app.MeetingRecorder", return_value=rec),
                patch.object(core, "transcribe", return_value="audio"),
            ):
                self.window.shortcut_capture("system")
                self.assertIn("audio del equipo", self.window.tray.toolTip())
                self.assertTrue(self.window.tray_blink_timer.isActive())
                self.window.shortcut_capture("system")
                self.wait_recording_stop()
                self.assertFalse(self.window.tray_blink_timer.isActive())
                deadline = time.monotonic() + 15
                while self.window.pending and time.monotonic() < deadline:
                    self.application.processEvents()
                    QTest.qWait(25)
                self.assertFalse(self.window.pending)
        finally:
            self.window.tray.hide()

    def test_system_shortcut_records_both_and_does_not_switch_live_recording(self):
        rec = self.fake_meeting_recorder()
        with (
            patch("backlogtalk.qt_app.system_output_device", return_value=23),
            patch("backlogtalk.qt_app.MeetingRecorder", return_value=rec),
            patch.object(core, "transcribe", return_value="Salida"),
        ):
            self.window.shortcut_capture("system")
            self.assertEqual(rec.devices[1], 23)
            self.assertEqual(self.window.recording_source, "system")
            self.assertIn("MIC + SALIDA", self.window.popup.title.text())
            self.window.shortcut_capture("mic")
            self.assertTrue(self.window.recording)
            self.assertFalse(rec.stopped)
            self.window.shortcut_capture("system")
            self.wait_recording_stop()
            self.assertTrue(rec.stopped)
            self.assertFalse(self.window.recording)
            deadline = time.monotonic() + 15
            while self.window.pending and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
        self.assertEqual(core.list_captures()[0]["transcript"], "Salida")
        self.assertEqual(core.list_captures()[0]["audio_source"], "meeting")
        self.assertIn("Reunión", self.window.history.item(0).text())

    def test_interrupted_mix_recovers_once_and_transcribes(self):
        from backlogtalk.audio_io import create_mix_job

        rec = self.fake_meeting_recorder()
        rec.start(None, 1)
        sources = rec.stop_sources()
        job = create_mix_job(sources, "DEMO", None)
        with patch.object(core, "transcribe", return_value="Reunión recuperada"):
            self.window.resume_pending()
            self.window.resume_pending()
            deadline = time.monotonic() + 15
            while self.window.pending and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
        rows = core.list_captures()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transcript"], "Reunión recuperada")
        self.assertEqual(rows[0]["audio_source"], "meeting")
        self.assertTrue(job.output.exists())
        self.assertFalse(job.manifest.exists())
        self.assertFalse(job.microphone.exists())
        self.assertFalse(job.system.exists())

    def test_interrupted_meeting_deletion_is_not_recreated(self):
        from backlogtalk.audio_io import create_mix_job

        rec = self.fake_meeting_recorder()
        rec.start(None, 1)
        job = create_mix_job(rec.stop_sources(), "DEMO", None)
        capture = core.add_capture("DEMO", None, audio=str(job.output), source="meeting")
        with patch.object(core, "finish_discarded_mix_files", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                core.delete_capture(capture)
        self.assertTrue(job.manifest.exists())
        self.window.resume_pending()
        self.assertEqual(core.list_captures(), [])
        self.assertFalse(job.manifest.exists())
        self.assertFalse(job.microphone.exists())
        self.assertFalse(job.system.exists())

    def test_shared_unmixed_recording_is_preserved_until_last_note_deleted(self):
        from backlogtalk.audio_io import create_mix_job

        rec = self.fake_meeting_recorder()
        rec.start(None, 1)
        job = create_mix_job(rec.stop_sources(), "DEMO", None)
        first = core.add_capture("DEMO", None, audio=str(job.output), source="meeting")
        second = core.add_capture("DEMO", None, audio=str(job.output), source="meeting")
        self.assertTrue(core.delete_capture(first, discard_manifest=job.manifest))
        self.assertTrue(job.manifest.exists())
        self.assertTrue(job.microphone.exists())
        self.assertTrue(job.system.exists())
        self.assertFalse(core.delete_capture(second))
        self.assertFalse(job.manifest.exists())

    def test_failed_mix_keeps_tracks_and_can_be_retried(self):
        rec = self.fake_meeting_recorder()
        with (
            patch("backlogtalk.qt_app.system_output_device", return_value=23),
            patch("backlogtalk.qt_app.MeetingRecorder", return_value=rec),
            patch.object(core, "transcribe", return_value="Recuperado"),
        ):
            with patch("backlogtalk.qt_app.mix_meeting", side_effect=OSError("sin espacio")):
                self.window.shortcut_capture("system")
                self.window.shortcut_capture("system")
                self.wait_recording_stop()
                deadline = time.monotonic() + 15
                while self.window.pending and time.monotonic() < deadline:
                    self.application.processEvents()
                    QTest.qWait(25)
            self.assertEqual(len(core.list_captures()), 1)
            self.assertIn("Pistas guardadas", self.window.history.item(0).text())
            self.window.history.setCurrentRow(0)
            self.window.retranscribe()
            deadline = time.monotonic() + 15
            while self.window.pending and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
        self.assertEqual(core.list_captures()[0]["transcript"], "Recuperado")
        self.assertEqual(len(core.list_captures()), 1)

    def test_import_media_saves_and_transcribes_without_overwriting_editor(self):
        import wave

        path = Path(self.tmp.name) / "import.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000)
        self.window.note.setPlainText("Nota que se está editando")
        with (
            patch("backlogtalk.qt_app.QFileDialog.getOpenFileName", return_value=(str(path), "")),
            patch.object(core, "transcribe", return_value="Archivo reconocido"),
        ):
            self.window.source.setCurrentIndex(self.window.source.findData("file"))
            self.window.toggle_record()
            deadline = time.monotonic() + 15
            while (self.window.busy or self.window.pending) and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.pending)
        self.assertEqual(core.list_captures()[0]["transcript"], "Archivo reconocido")
        self.assertEqual(core.list_captures()[0]["audio_source"], "file")
        self.assertIn("Archivo", self.window.history.item(0).text())
        self.assertEqual(self.window.note.toPlainText(), "Nota que se está editando")
        self.assertTrue(path.exists())

    def test_cancel_file_transcription_keeps_audio_and_is_not_resumed(self):
        import threading

        capture = core.add_capture(
            "DEMO", None, audio=str(core.AUDIO_DIR / "saved.wav"), source="file"
        )
        path = core.AUDIO_DIR / "saved.wav"
        path.write_bytes(b"audio")
        started = threading.Event()

        def transcribe(_path, _model, **kwargs):
            started.set()
            kwargs["cancel"].wait(3)
            raise core.TranscriptionCancelled()

        with (
            patch.object(core, "transcribe", side_effect=transcribe),
            patch.object(self.window, "error") as error,
        ):
            self.window.queue_transcription(capture, path)
            deadline = time.monotonic() + 5
            while (
                not started.is_set() or self.window.running_id != capture
            ) and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)
            self.assertEqual(self.window.running_id, capture)
            self.assertTrue(self.window.cancel_transcription_button.isVisible())
            self.window.cancel_transcription_button.click()
            while self.window.pending and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)
            error.assert_not_called()
            self.assertFalse(self.window.pending)
            self.assertTrue(path.exists())
            self.assertEqual(self.window.get_capture(capture)["state"], "cancelled")
            self.window.resume_pending()
            self.assertNotIn(capture, self.window.pending)

    def test_file_import_is_blocked_while_transcription_is_pending(self):
        self.window.pending.add(42)
        try:
            with (
                patch.object(self.window, "info") as info,
                patch.object(qt_app.QFileDialog, "getOpenFileName") as chooser,
            ):
                self.window.choose_media()
                chooser.assert_not_called()
                info.assert_called_once()
        finally:
            self.window.pending.clear()

    def test_cancel_manual_retranscription_keeps_previous_text_even_if_result_arrives(self):
        import threading

        audio = core.AUDIO_DIR / "old.wav"
        audio.write_bytes(b"audio")
        capture = core.add_capture("DEMO", None, "Texto anterior", str(audio))
        self.window.refresh()
        self.window.show_page(2)
        self.window.history.setCurrentRow(0)
        started = threading.Event()

        def delayed(_path, _model, **kwargs):
            started.set()
            kwargs["cancel"].wait(3)
            return "Resultado tardío"

        with (
            patch.object(core, "transcribe", side_effect=delayed),
            patch.object(self.window, "confirm", return_value=True),
            patch.object(self.window, "error") as error,
        ):
            self.window.retranscribe()
            deadline = time.monotonic() + 5
            while not started.is_set() and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)
            self.assertTrue(started.is_set())
            self.assertTrue(self.window.cancel_transcription_button.isVisible())
            self.window.cancel_transcription()
            while self.window.busy and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)
            self.assertFalse(self.window.busy)
            error.assert_not_called()
        self.assertEqual(self.window.get_capture(capture)["transcript"], "Texto anterior")
        self.assertTrue(audio.exists())

    def test_system_shortcut_can_be_recorded_and_saved(self):
        self.window.show_page(3)
        self.window.toggle_system_shortcut_mode()
        self.application.processEvents()
        QTest.keyClick(
            self.window.shortcut_system_keys,
            Qt.Key.Key_L,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        self.application.processEvents()
        self.assertEqual(self.window.shortcut_system_edit.text(), "Ctrl+Shift+L")
        self.window.toggle_system_shortcut_mode()
        self.window.save_settings()
        self.assertEqual(core.load_config()["shortcut_system"], "Ctrl+Shift+L")

    def test_existing_microphone_shortcut_does_not_conflict_with_new_default(self):
        config = dict(core.load_config(), shortcut="Ctrl+Alt+K", shortcut_system="Ctrl+Alt+K")
        with patch.object(core, "load_config", return_value=config):
            other = qt_app.VoiceWindow()
        try:
            self.assertNotEqual(other.shortcut.key(), other.system_shortcut.key())
            self.assertEqual(other.system_shortcut.key().toString(), "Ctrl+Alt+L")
        finally:
            other.timer.stop()
            other.jobs.put((2, next(other.order), None, None, None))
            other.popup.deleteLater()
            other.deleteLater()

    def test_open_shortcut_is_configurable_and_cannot_duplicate_other_bindings(self):
        from PySide6.QtGui import QKeySequence

        self.window.shortcut_open_edit.setText("Ctrl+Shift+B")
        self.assertTrue(self.window.save_settings())
        self.assertEqual(core.load_config()["shortcut_open"], "Ctrl+Shift+B")
        self.assertEqual(
            self.window.open_shortcut.key().toString(QKeySequence.SequenceFormat.PortableText),
            "Ctrl+Shift+B",
        )
        self.window.shortcut_open_edit.setText("Ctrl+Alt+J")
        with patch.object(self.window, "error") as error:
            self.assertFalse(self.window.save_settings())
            error.assert_called_once()
        self.assertEqual(core.load_config()["shortcut_open"], "Ctrl+Shift+B")

    def test_open_shortcut_can_be_recorded_and_is_ignored_while_editing(self):
        self.window.show_page(3)
        self.window.toggle_open_shortcut_mode()
        self.application.processEvents()
        QTest.keyClick(
            self.window.shortcut_open_keys,
            Qt.Key.Key_B,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        self.application.processEvents()
        self.assertEqual(self.window.shortcut_open_edit.text(), "Ctrl+Shift+B")
        with (
            patch.object(self.window, "shortcut_editor_focused", return_value=True),
            patch.object(self.window, "open_with_history") as open_history,
        ):
            self.window.shortcut_open_requested()
            open_history.assert_not_called()
        self.window.toggle_open_shortcut_mode()
        self.assertTrue(self.window.save_settings())
        self.assertEqual(core.load_config()["shortcut_open"], "Ctrl+Shift+B")

    def test_open_shortcut_falls_back_if_existing_binding_conflicts(self):
        config = dict(core.load_config(), shortcut="Ctrl+Alt+B", shortcut_open="Ctrl+Alt+B")
        with patch.object(core, "load_config", return_value=config):
            other = qt_app.VoiceWindow()
        try:
            self.assertNotEqual(other.open_shortcut.key(), other.shortcut.key())
            self.assertNotEqual(other.open_shortcut.key(), other.system_shortcut.key())
        finally:
            other.timer.stop()
            other.jobs.put((2, next(other.order), None, None, None))
            other.popup.deleteLater()
            other.deleteLater()

    def test_two_shortcuts_cannot_be_the_same(self):
        self.window.shortcut_system_edit.setText("Ctrl+Alt+J")
        with patch.object(self.window, "error") as error:
            self.assertFalse(self.window.save_settings())
            error.assert_called_once()
        self.assertEqual(core.load_config()["shortcut_system"], "Ctrl+Alt+K")

    def test_selection_anchor_survives_refresh(self):
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture,
            [
                {"title": f"Tarea ejemplo {i}", "description": "Descripción", "type": "Task"}
                for i in range(4)
            ],
        )
        self.window.refresh()
        self.window.show_page(1)
        self.application.processEvents()
        self.click_row(self.window.tickets, 1, Qt.KeyboardModifier.ControlModifier)
        anchor = self.window.tickets.currentItem().data(Qt.ItemDataRole.UserRole)
        self.window.refresh()
        self.assertEqual(self.window.tickets.currentItem().data(Qt.ItemDataRole.UserRole), anchor)
        self.click_row(self.window.tickets, 3, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(len(self.window.selected_ids(self.window.tickets)), 3)

    def test_uncertain_bulk_delete_is_blocked(self):
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture,
            [
                {"title": f"Tarea ejemplo {i}", "description": "Descripción", "type": "Task"}
                for i in range(2)
            ],
        )
        ticket = core.list_tickets()[0]
        with core.db_connect() as db:
            db.execute("UPDATE tickets SET state='uncertain' WHERE id=?", (ticket["id"],))
        self.window.refresh()
        self.window.show_page(1)
        self.window.tickets.item(0).setSelected(True)
        self.window.tickets.item(1).setSelected(True)
        with (
            patch.object(self.window, "info") as info,
            patch.object(self.window, "confirm") as confirm,
        ):
            self.window.delete_tickets()
            info.assert_called_once()
            confirm.assert_not_called()
        self.assertEqual(len(core.list_tickets()), 2)

    def test_edit_during_automatic_transcription_is_preserved(self):
        capture = core.add_capture("DEMO", None, audio="unused.wav")
        self.window.capture_id = capture
        self.window.note.setPlainText("Corrección manual")
        self.window.pending.add(capture)
        self.window.results.put(("asr_done", capture, "Texto automático"))
        self.window.poll()
        self.assertEqual(self.window.note.toPlainText(), "Corrección manual")
        self.window.save_note()
        self.assertEqual(core.list_captures()[0]["transcript"], "Corrección manual")

    def test_epic_manager_fetches_and_saves_only_checked_with_local_alias(self):
        self.window.config["jira_contexts"] = {
            "example.atlassian.net": {"DEMO": {"epics": {"DEMO-2252": "Regla conservada"}}}
        }
        scope = core.epic_favorites(self.window.config, "example.atlassian.net")
        manager = qt_app.EpicManager(
            self.window,
            "DEMO",
            "example.atlassian.net",
            scope.get("projects", {}).get("DEMO", []),
            scope.get("names", {}),
            lambda selected, names: self.window.save_epic_favorites(
                "example.atlassian.net", "DEMO", selected, names
            ),
        )
        manager.show()
        self.window.epic.setCurrentText("DEMO-414")
        with patch.object(
            core,
            "jira_epics",
            return_value=[
                {"key": "DEMO-414", "summary": "Original"},
                {"key": "DEMO-99", "summary": "Desde Jira"},
            ],
        ):
            manager.fetch_epics()
            deadline = time.monotonic() + 3
            while not manager.fetch.isEnabled() and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(20)
        self.assertTrue(manager.fetch.isEnabled())
        self.assertEqual(manager.table.rowCount(), 4)
        manager.table.item(0, 1).setText("Nombre corto")
        manager.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        manager.table.item(3, 0).setCheckState(Qt.CheckState.Checked)
        manager.search.setText("no coincide")
        manager.save()
        config = core.load_config()
        scope = core.epic_favorites(config, "example.atlassian.net")
        self.assertEqual(scope["projects"]["DEMO"], ["DEMO-414", "DEMO-1245", "DEMO-99"])
        self.assertEqual(scope["names"]["DEMO-414"], "Nombre corto")
        self.assertEqual(scope["names"]["DEMO-99"], "Desde Jira")
        self.assertEqual(
            core.jira_instructions(config, "example.atlassian.net", "DEMO", "DEMO-2252")[1],
            "Regla conservada",
        )
        self.assertEqual(self.window.epic_key(), "DEMO-414")
        self.assertEqual(self.window.epic.currentText(), "Nombre corto / DEMO-414")
        self.assertEqual(self.window.epic.findText("DEMO-2252"), -1)
        manager.deleteLater()

    def test_favorite_epics_are_scoped_to_site_and_legacy_data_is_preserved(self):
        first_site = "example.atlassian.net"
        second_site = "other.atlassian.net"
        self.assertEqual(
            core.epic_favorites(self.window.config, first_site)["projects"]["DEMO"],
            ["DEMO-414", "DEMO-2252", "DEMO-1245"],
        )
        self.window.settings["jira_site"].setText(second_site)
        self.assertTrue(self.window.save_settings())
        self.assertEqual(self.window.epic.count(), 1)
        self.window.save_epic_favorites(
            second_site, "DEMO", ["DEMO-414"], {"DEMO-414": "Otro sitio"}
        )
        self.assertEqual(self.window.epic.itemText(1), "Otro sitio / DEMO-414")
        self.window.settings["jira_site"].setText(first_site)
        self.assertTrue(self.window.save_settings())
        self.assertNotIn(
            "Otro sitio", [self.window.epic.itemText(i) for i in range(self.window.epic.count())]
        )
        config = core.load_config()
        self.assertEqual(
            core.epic_favorites(config, second_site)["names"]["DEMO-414"], "Otro sitio"
        )
        self.assertEqual(len(core.epic_favorites(config, first_site)["projects"]["DEMO"]), 3)

    def test_epic_manager_stops_timer_when_closed(self):
        manager = qt_app.EpicManager(
            self.window, "DEMO", "example.atlassian.net", [], {}, lambda *_: None
        )
        manager.show()
        self.assertTrue(manager.timer.isActive())
        manager.reject()
        self.assertFalse(manager.timer.isActive())
        manager.deleteLater()

    def test_cancel_error_keeps_recovery_operation_when_writer_is_open(self):
        recorder = qt_app.VoiceRecorder()
        recorder.writer = object()
        recorder.stream = None
        self.window.recorder = recorder
        self.window.recording = True
        self.window.record_operation = "operation-id"
        with (
            patch.object(core, "finish_operation") as finish,
            patch.object(self.window, "show_media_failure") as show_error,
        ):
            self.window.finish_stop_record(
                (True, "mic"),
                {
                    "error": "disk error",
                    "details": "partial.wav",
                    "cancelling": True,
                },
            )
        finish.assert_not_called()
        self.assertEqual(self.window.record_operation, "operation-id")
        self.assertIn("partial.wav", show_error.call_args.args[2])
        self.assertFalse(self.window.recording)

    def test_new_project_resets_previous_epic(self):
        self.window.epic.setCurrentText("DEMO-414")
        self.window.project.setCurrentText("OTRO")
        self.assertEqual(self.window.epic_key(), "")

    def test_instance_shortcut_toggles_record_and_transcribes(self):
        from test_instance import send_from_process

        from backlogtalk.instance import Instance

        class Recorder:
            level = 0

            def start(self, device):
                pass

            def stop_to_file(self):
                path = core.AUDIO_DIR / "shortcut.wav"
                path.write_bytes(b"audio")
                return path

        self.window.recorder = Recorder()
        root = Path(self.tmp.name) / "unique-project"
        first = Instance(root)
        self.assertTrue(first.acquire_or_send("open"))
        self.window.instance = first
        try:
            with patch.object(core, "transcribe", return_value="Texto del atajo"):
                for action in ("start", "stop"):
                    send_from_process(root, "capture", self.window.poll)
                    deadline = time.monotonic() + 3
                    while (
                        self.window.recording != (action == "start") and time.monotonic() < deadline
                    ):
                        self.application.processEvents()
                        self.window.poll()
                        QTest.qWait(25)
                    if action == "stop":
                        self.wait_recording_stop()
                    self.assertEqual(self.window.recording, action == "start")
                deadline = time.monotonic() + 4
                while self.window.pending and time.monotonic() < deadline:
                    self.application.processEvents()
                    QTest.qWait(25)
            self.assertEqual(core.list_captures()[0]["transcript"], "Texto del atajo")
        finally:
            self.window.instance = None
            first.close()

    def test_quick_capture_keeps_current_editor_and_saves_independently(self):
        class Recorder:
            level = 0

            def start(self, device):
                pass

            def stop_to_file(self):
                path = core.AUDIO_DIR / "quick.wav"
                path.write_bytes(b"audio")
                return path

        self.window.recorder = Recorder()
        self.window.note.setPlainText("Edición pendiente")
        self.window.show_page(3)
        with patch.object(core, "transcribe", return_value="Nota rápida"):
            self.window.shortcut_capture()
            self.assertTrue(self.window.popup.isVisible())
            self.assertEqual(self.window.pages.currentIndex(), 3)
            self.window.shortcut_capture()
            self.wait_recording_stop()
            deadline = time.monotonic() + 4
            while self.window.pending and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
        self.assertEqual(self.window.note.toPlainText(), "Edición pendiente")
        self.assertIsNone(self.window.capture_id)
        self.assertEqual(core.list_captures()[0]["transcript"], "Nota rápida")
        self.assertIn("NOTA GUARDADA", self.window.popup.title.text())
        self.assertEqual(self.window.pages.currentIndex(), 3)
        self.window.open_with_history()
        self.assertEqual(self.window.pages.currentIndex(), 2)
        self.assertEqual(self.window.selected_ids(self.window.history), [self.window.quick_id])
        self.assertEqual(self.window.note.toPlainText(), "Edición pendiente")

    def test_open_history_selects_quick_note_while_transcription_runs(self):
        import threading

        class Recorder:
            level = 0

            def start(self, device):
                pass

            def stop_to_file(self):
                path = core.AUDIO_DIR / "quick-pending.wav"
                path.write_bytes(b"audio")
                return path

        started = threading.Event()
        finish = threading.Event()

        def transcribe(*_args, **_kwargs):
            started.set()
            finish.wait(5)
            return "Transcripción terminada"

        self.window.recorder = Recorder()
        try:
            with patch.object(core, "transcribe", side_effect=transcribe):
                self.window.shortcut_capture()
                self.assertTrue(self.window.recording)
                self.window.shortcut_capture()
                self.wait_recording_stop()
                self.assertTrue(started.wait(3))
                self.assertIn(self.window.quick_id, self.window.pending)
                self.window.open_with_history()
                self.assertEqual(self.window.pages.currentIndex(), 2)
                self.assertEqual(
                    self.window.selected_ids(self.window.history), [self.window.quick_id]
                )
                finish.set()
                deadline = time.monotonic() + 4
                while self.window.pending and time.monotonic() < deadline:
                    self.application.processEvents()
                    QTest.qWait(25)
                self.assertFalse(self.window.pending)
                self.assertEqual(
                    self.window.selected_ids(self.window.history), [self.window.quick_id]
                )
        finally:
            finish.set()

    def test_cancel_recording_requires_confirmation_and_leaves_no_note(self):
        class Recorder:
            level = 0
            path = None

            def start(self, device):
                self.path = core.AUDIO_DIR / "cancel-recording.wav"
                self.path.write_bytes(b"audio")

            def discard(self):
                self.path.unlink()

        for quick in (False, True):
            with self.subTest(quick=quick):
                recorder = Recorder()
                self.window.recorder = recorder
                self.window.start_record(quick=quick)
                self.assertTrue(self.window.recording)
                self.assertFalse(self.window.cancel_record_button.isHidden())
                with patch.object(self.window, "confirm", side_effect=[False, True]) as confirm:
                    self.window.cancel_record()
                    self.assertTrue(self.window.recording)
                    self.assertTrue(recorder.path.exists())
                    if quick:
                        self.window.popup.cancel.click()
                    else:
                        self.window.cancel_record_button.click()
                    self.wait_recording_stop()
                    self.assertEqual(confirm.call_count, 2)
                self.assertFalse(self.window.recording)
                self.assertFalse(recorder.path.exists())
                self.assertEqual(core.list_captures(), [])
                self.assertFalse(self.window.pending)
                self.assertTrue(self.window.cancel_record_button.isHidden())

    def test_shortcut_cannot_stop_recording_while_cancel_confirmation_is_open(self):

        class Recorder:
            level = 0
            path = None

            def start(self, device):
                self.path = core.AUDIO_DIR / "pending-confirmation.wav"
                self.path.write_bytes(b"audio")

            def discard(self):
                self.path.unlink()

        self.window.recorder = Recorder()
        self.window.start_record(quick=True)
        seen = []

        def during_confirmation():
            dialog = self.application.activeModalWidget()
            try:
                self.window.shortcut_capture()
                self.window.shortcut_capture("system")
                self.window.stop_record()
                self.window.cancel_record()

                class Instance:
                    def receive(self):
                        return ["capture", "capture-system"]

                self.window.instance = Instance()
                self.window.poll()
                self.window.instance = None
                seen.append(
                    (
                        self.window.recording,
                        self.window.recording_stopping,
                        len(core.list_captures()),
                    )
                )
                self.window.show_media_failure("Aviso de otra tarea", "Detalle", "diagnóstico")
                seen.append((len(self.window.failure_windows), len(self.window.deferred_failures)))
            finally:
                self.window.instance = None
                next(button for button in dialog.buttons() if button.text() == "Cancelar").click()

        QTimer.singleShot(0, during_confirmation)
        self.window.cancel_record()
        self.assertEqual(seen, [(True, False, 0), (0, 1)])
        self.assertTrue(self.window.recording)
        self.window.poll()
        self.assertEqual(len(self.window.failure_windows), 1)
        next(iter(self.window.failure_windows)).close()
        with patch.object(self.window, "confirm", return_value=True):
            self.window.cancel_record()
            self.wait_recording_stop()
        self.assertFalse(self.window.recording)
        self.assertEqual(core.list_captures(), [])

    def test_capture_only_without_tray_remains_visible_until_transcription_finishes(self):
        self.window.hide()
        self.window.capture_only = True
        self.window.pending.add(7)
        self.window.popup.saved(7)
        self.window.popup.show()
        self.window.popup.stop_or_close()
        self.assertTrue(self.window.popup.isVisible())
        self.assertFalse(self.window.closing)
        self.window.pending.clear()
        self.window.popup.transcription_finished(False)
        self.assertTrue(self.window.popup.isVisible())
        self.assertIn("No se pudo transcribir", self.window.popup.message.text())

    def test_tray_close_hides_and_retains_background_operation(self):
        self.window.busy = True
        with patch.object(self.window, "tray_available", return_value=True):
            self.assertFalse(self.window.close())
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.closing)
        self.assertTrue(self.window.busy)
        self.window.busy = False

    def test_failed_transcription_shows_audio_saved(self):
        capture = core.add_capture("DEMO", None, audio="unused.wav")
        self.window.quick_id = capture
        self.window.capture_only = True
        self.window.pending.add(capture)
        self.window.results.put(("asr_error", capture, "falló"))
        self.window.poll()
        self.assertIn("AUDIO GUARDADO", self.window.popup.title.text())
        self.assertIn("No se pudo transcribir", self.window.popup.message.text())
        self.assertEqual(len(core.list_captures()), 1)

    def test_async_error_is_simple_copyable_and_persisted_in_history(self):
        from PySide6.QtWidgets import QPushButton, QTextEdit

        capture = core.add_capture("DEMO", None, audio="missing.wav")
        self.window.pending.add(capture)
        self.window.running_id = capture
        self.window.results.put(
            (
                "asr_error",
                capture,
                {
                    "message": "decoder failure",
                    "details": "Traceback de prueba: decoder failure",
                    "operation": None,
                },
            )
        )
        self.window.poll()
        self.application.processEvents()
        error = core.get_capture_error(capture)
        self.assertIsNotNone(error)
        self.assertIn("decoder failure", error["details"])
        self.assertEqual(len(self.window.failure_windows), 1)
        dialog = next(iter(self.window.failure_windows))
        self.assertIn("No se pudo procesar", dialog.windowTitle())
        buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
        buttons["Ver detalles"].click()
        self.application.processEvents()
        detail = dialog.findChild(QTextEdit)
        self.assertTrue(detail.isVisible())
        buttons["Copiar detalles"].click()
        copied = self.application.clipboard().text()
        for text in (
            "Traceback de prueba",
            "Sistema:",
            "Python:",
            "Qt:",
            "PortAudio:",
            "Bibliotecas FFmpeg:",
            "faster-whisper:",
            "sounddevice:",
            "av:",
        ):
            self.assertIn(text, copied)
        self.window.show_page(2)
        self.window.history.setCurrentRow(0)
        self.application.processEvents()
        self.assertFalse(self.window.error_details_button.isHidden())
        dialog.close()

    def test_empty_transcription_warns_and_preserves_audio(self):
        audio = core.AUDIO_DIR / "silent.wav"
        audio.write_bytes(b"audio")
        capture = core.add_capture("DEMO", None, audio=str(audio))
        self.window.pending.add(capture)
        self.window.running_id = capture
        self.window.results.put(("asr_done", capture, {"text": "", "operation": None}))
        self.window.poll()
        error = core.get_capture_error(capture)
        self.assertEqual(error["kind"], "sin_voz")
        self.assertTrue(audio.exists())
        self.assertTrue(
            any(d.windowTitle() == "No se detectó voz" for d in self.window.failure_windows)
        )

    def test_media_diagnostics_include_audio_format_without_private_path(self):
        import wave

        path = core.AUDIO_DIR / "private-name.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000)
        capture = core.add_capture("DEMO", None, audio=str(path))
        details = self.window.media_diagnostics("No se detectó voz", f"Nota: #{capture}\nSin voz")
        self.assertIn("1.0 s, 16000 Hz, 1 canal(es), 16 bits", details)
        self.assertNotIn(str(path), details)
        self.assertIn("Python:", details)
        self.assertIn("faster-whisper:", details)

    def test_interrupted_import_is_reported_after_restart(self):
        operation = core.begin_operation("importación", details="Archivo original: reunión.mp4")
        with core.db_connect() as db:
            db.execute(
                "UPDATE processing_operations SET owner_pid=?, owner_started=NULL WHERE operation_id=?",
                (2147483647, operation),
            )
        self.window.recovery_checked = False
        self.window.resume_pending()
        self.assertTrue(
            any(d.windowTitle() == "Proceso interrumpido" for d in self.window.failure_windows)
        )
        with core.db_connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM processing_operations").fetchone()[0], 0
            )

    def test_video_import_error_is_reported_without_deleting_original(self):
        source = Path(self.tmp.name) / "broken.mp4"
        source.write_bytes(b"bad input")
        self.window.source.setCurrentIndex(self.window.source.findData("file"))
        with patch(
            "backlogtalk.qt_app.QFileDialog.getOpenFileName", return_value=(str(source), "")
        ):
            self.window.toggle_record()
            deadline = time.monotonic() + 15
            while self.window.busy and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
        self.assertFalse(self.window.busy)
        self.assertTrue(source.exists())
        self.assertTrue(
            any(
                d.windowTitle() == "No se pudo procesar el audio"
                for d in self.window.failure_windows
            )
        )
        with core.db_connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM processing_operations").fetchone()[0], 0
            )

    def test_audio_language_is_saved_and_passed_to_transcription(self):
        capture = core.add_capture("DEMO", None, audio="test.wav")
        self.window.transcription_language.setCurrentIndex(
            self.window.transcription_language.findData("en")
        )
        self.assertTrue(self.window.save_settings())
        self.assertEqual(core.load_config()["transcription_language"], "en")
        with patch.object(core, "transcribe", return_value="English") as transcribe:
            self.window.queue_transcription(capture, Path("test.wav"))
            deadline = time.monotonic() + 10
            while self.window.pending and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
            self.assertFalse(self.window.pending)
            self.assertEqual(transcribe.call_args.kwargs["language"], "en")
        self.assertEqual(core.list_captures()[0]["transcript"], "English")

    def test_model_settings_show_active_backend_and_persist_selection(self):
        self.window.show_page(3)
        self.application.processEvents()
        self.assertFalse(self.window.cursor_row.isHidden())
        self.assertTrue(self.window.ollama_row.isHidden())
        self.window.whisper_model.setCurrentIndex(self.window.whisper_model.findData("medium"))
        self.window.cursor_model.setCurrentText("composer-2.5")
        self.window.save_settings()
        self.assertEqual(core.load_config()["whisper_model"], "medium")
        self.assertEqual(core.load_config()["cursor_model"], "composer-2.5")
        self.window.backend.setCurrentText("ollama")
        self.assertTrue(self.window.cursor_row.isHidden())
        self.assertFalse(self.window.ollama_row.isHidden())
        self.assertEqual(
            self.window.settings["ollama_model"].text(), core.DEFAULT_CONFIG["ollama_model"]
        )

    def test_loading_models_selects_composer_when_available(self):
        self.window.cursor_model.setCurrentText("other-model")
        with (
            patch.object(
                core, "list_cursor_models", return_value=(["auto", "composer-2.5"], "auto")
            ),
            patch.object(
                self.window, "background", side_effect=lambda fn, done, status: done(fn())
            ),
        ):
            self.window.load_cursor_models()
        self.assertEqual(self.window.cursor_model.currentText(), "composer-2.5")
        self.assertIn("automáticamente", self.window.status.text())

    def test_loading_models_preserves_selection_if_composer_unavailable(self):
        self.window.cursor_model.setCurrentText("my-model")
        with (
            patch.object(core, "list_cursor_models", return_value=(["auto"], None)),
            patch.object(
                self.window, "background", side_effect=lambda fn, done, status: done(fn())
            ),
        ):
            self.window.load_cursor_models()
        self.assertEqual(self.window.cursor_model.currentText(), "my-model")
        self.assertIn("no está disponible", self.window.status.text())

    def test_shortcut_lookup_failure_falls_back_to_app_bindings(self):
        deadline = time.monotonic() + 2
        while self.window.shortcut_lookup_running and time.monotonic() < deadline:
            self.window.poll()
            QTest.qWait(10)
        with patch.object(
            qt_app.shortcuts, "gnome_global_shortcuts", side_effect=RuntimeError("no disponible")
        ):
            self.window.refresh_shortcut_summary()
            deadline = time.monotonic() + 2
            while self.window.shortcut_lookup_running and time.monotonic() < deadline:
                self.window.poll()
                QTest.qWait(10)
        self.assertFalse(self.window.shortcut_lookup_running)
        self.assertIn("Mic (app)", self.window.shortcut_value.text())
        self.assertIn("Abrir (app)", self.window.shortcut_value.text())

    def test_shortcut_summary_shows_observed_global_bindings(self):
        self.window.display_shortcut_summary(
            {"mic": ["Ctrl+Shift+M"], "meeting": [], "open": ["Super+B"]}
        )
        self.assertIn("Mic  Ctrl+Shift+M", self.window.shortcut_value.text())
        self.assertIn("Reunión  Sin asignar", self.window.shortcut_value.text())
        self.assertIn("Abrir  Super+B", self.window.shortcut_value.text())
        self.assertIn("globales", self.window.shortcut_value.toolTip())

    def test_application_shortcut_can_be_changed_and_saved(self):
        from PySide6.QtGui import QKeySequence

        self.window.shortcut_edit.setText("Ctrl+Shift+K")
        self.window.save_settings()
        self.assertEqual(core.load_config()["shortcut"], "Ctrl+Shift+K")
        self.assertEqual(
            self.window.shortcut.key().toString(QKeySequence.SequenceFormat.PortableText),
            "Ctrl+Shift+K",
        )
        self.window.display_shortcut_summary(None)
        self.assertIn(
            self.window.shortcut.key().toString(QKeySequence.SequenceFormat.NativeText),
            self.window.shortcut_value.text(),
        )
        self.assertIn("Mic (app)", self.window.shortcut_value.text())

    def test_shortcut_can_be_typed_without_firing_the_existing_binding(self):
        self.window.show_page(3)
        self.window.shortcut_edit.setFocus()
        self.window.shortcut_edit.clear()
        QTest.keyClicks(self.window.shortcut_edit, "Ctrl+Alt+J")
        self.assertEqual(self.window.shortcut_edit.text(), "Ctrl+Alt+J")
        self.assertFalse(self.window.recording)
        self.window.save_settings()
        self.assertEqual(core.load_config()["shortcut"], "Ctrl+Alt+J")

    def test_shortcut_can_be_recorded_and_edited_as_text(self):
        self.window.show_page(3)
        self.window.toggle_shortcut_mode()
        self.application.processEvents()
        self.assertEqual(self.window.shortcut_stack.currentIndex(), 1)
        QTest.keyClick(
            self.window.shortcut_keys,
            Qt.Key.Key_K,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        self.application.processEvents()
        self.assertEqual(self.window.shortcut_edit.text(), "Ctrl+Shift+K")
        self.window.toggle_shortcut_mode()
        self.assertEqual(self.window.shortcut_edit.text(), "Ctrl+Shift+K")
        self.window.save_settings()
        self.assertEqual(core.load_config()["shortcut"], "Ctrl+Shift+K")

    def test_shortcut_trigger_is_ignored_during_configuration(self):
        self.window.show_page(3)
        self.window.activateWindow()
        self.window.toggle_shortcut_mode()
        self.application.processEvents()
        with (
            patch.object(self.window, "shortcut_editor_focused", return_value=True),
            patch.object(self.window, "start_record") as start,
        ):
            self.window.shortcut_capture()
            start.assert_not_called()
        self.assertIn("Escribir", self.window.shortcut_help.text())

    def test_invalid_shortcut_does_not_replace_working_binding(self):
        for text in ("Ctrl+Alt", "Ctrl+????", "Ctrl+K, Ctrl+C"):
            self.window.shortcut_edit.setText(text)
            with patch.object(self.window, "error") as error:
                self.window.save_settings()
                error.assert_called_once()
            self.assertEqual(self.window.shortcut.key().toString(), "Ctrl+Alt+J")

    def test_moving_is_blocked_while_audio_is_being_processed(self):
        self.window.pending.add(1)
        with (
            patch.object(self.window, "info") as info,
            patch("backlogtalk.qt_app.QFileDialog.getExistingDirectory") as folder,
        ):
            self.window.move_storage()
        info.assert_called_once()
        folder.assert_not_called()
        self.window.pending.clear()

    def test_move_data_from_settings_preserves_note_and_activates_destination(self):
        import tempfile

        self.window.note.setPlainText("Nota para conservar")
        source = core.APP_HOME
        with (
            tempfile.TemporaryDirectory() as parent,
            patch.object(core, "DB_FILE", core.APP_HOME / "inbox.sqlite3"),
            patch.object(core, "LOCATION_FILE", Path(self.tmp.name) / "location.json"),
            patch("backlogtalk.qt_app.QFileDialog.getExistingDirectory", return_value=parent),
            patch.object(self.window, "confirm", return_value=True),
            patch.object(self.window, "info"),
            patch.object(self.window, "error") as error,
        ):
            core.db_connect().close()
            self.window.move_storage()
            deadline = time.monotonic() + 5
            while self.window.busy and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(25)
            self.assertFalse(self.window.busy)
            error.assert_not_called()
            self.assertEqual(core.APP_HOME, Path(parent).resolve() / "BacklogTalk-data" / "data")
            self.assertEqual(core.list_captures()[0]["transcript"], "Nota para conservar")
            self.assertTrue((source / "db.sqlite3").exists())
            self.assertIn(str(core.APP_HOME), self.window.history_location.text())

    def test_settings_are_saved_without_a_button_and_keep_selected_epic(self):
        self.window.epic.setCurrentText("DEMO-414")
        self.window.show_page(3)
        self.window.whisper_model.setCurrentIndex(self.window.whisper_model.findData("medium"))
        self.window.settings["ollama_model"].setText("modelo-local")
        QTest.qWait(1000)
        config = core.load_config()
        self.assertEqual(config["whisper_model"], "medium")
        self.assertEqual(config["ollama_model"], "modelo-local")
        self.assertEqual(self.window.epic_key(), "DEMO-414")
        self.assertFalse(self.window.settings_dirty)

    def test_invalid_autosave_keeps_previous_config_and_reports_without_popup(self):
        self.window.shortcut_edit.setText("Ctrl+????")
        with patch.object(self.window, "error") as error:
            QTest.qWait(1000)
            error.assert_not_called()
        self.assertEqual(core.load_config()["shortcut"], "Ctrl+Alt+J")
        self.assertTrue(self.window.settings_dirty)
        self.assertIn("sin guardar", self.window.status.text())
        self.window.shortcut_edit.setText("Ctrl+Shift+K")
        QTest.qWait(1000)
        self.assertEqual(core.load_config()["shortcut"], "Ctrl+Shift+K")
        self.assertFalse(self.window.settings_dirty)

    def test_leaving_settings_saves_pending_changes_immediately(self):
        self.window.show_page(3)
        self.window.whisper_model.setCurrentIndex(self.window.whisper_model.findData("medium"))
        self.assertTrue(self.window.settings_dirty)
        self.assertTrue(self.window.show_page(0))
        self.assertEqual(core.load_config()["whisper_model"], "medium")
        self.assertFalse(self.window.settings_dirty)

    def test_autosave_write_failure_keeps_last_good_settings(self):
        before = core.load_config()
        self.window.whisper_model.setCurrentIndex(self.window.whisper_model.findData("medium"))
        with (
            patch.object(core, "save_config", side_effect=OSError("sin espacio")),
            patch.object(self.window, "error") as error,
        ):
            QTest.qWait(1000)
            error.assert_not_called()
        self.assertEqual(core.load_config(), before)
        self.assertTrue(self.window.settings_dirty)
        self.assertIn("sin guardar", self.window.status.text())
        self.assertEqual(self.window.config["whisper_model"], before["whisper_model"])

    def test_leaving_skill_saves_pending_edit_immediately(self):
        self.window.open_skill()
        self.window.skill.setPlainText("Instrucción guardada al salir")
        self.assertTrue(self.window.show_page(3))
        self.assertEqual(core.load_skill().strip(), "Instrucción guardada al salir")

    def test_skill_edits_are_saved_automatically(self):
        self.window.open_skill()
        self.window.skill.setPlainText("Formato de ticket personalizado")
        QTest.qWait(1000)
        self.assertEqual(core.load_skill().strip(), "Formato de ticket personalizado")
        self.assertTrue(self.window.show_page(3))

    def test_new_note_does_not_select_an_epic_by_default(self):
        self.window.epic.setCurrentText("DEMO-414")
        self.window.note.setPlainText("Nota anterior")
        self.window.new_capture()
        self.assertEqual(core.list_captures()[0]["epic_key"], "DEMO-414")
        self.assertEqual(self.window.epic_key(), "")

    def test_custom_whisper_model_is_preserved(self):
        self.window.config["whisper_model"] = "custom/model"
        self.window.whisper_model.addItem("custom/model - Personalizado", "custom/model")
        self.window.whisper_model.setCurrentIndex(
            self.window.whisper_model.findData("custom/model")
        )
        self.window.save_settings()
        self.assertEqual(core.load_config()["whisper_model"], "custom/model")

    def test_progress_displays_measured_fraction_estimate_and_resources(self):
        self.window.pending.add(9)
        self.window.running_id = 9
        self.window.asr_progress = (30.0, 120.0, 10.0)
        self.window.resource_last_sample = 0
        self.window.update_progress()
        self.application.processEvents()
        self.assertTrue(self.window.progress_panel.isVisible())
        self.assertEqual(self.window.progress.value(), 25)
        self.assertIn("25% del audio", self.window.progress_label.text())
        self.assertIn("faltan ~30 s", self.window.progress_label.text())
        self.assertIn("CPU", self.window.system_resource_label.text())
        self.assertIn("RAM", self.window.system_resource_label.text())
        self.assertFalse(hasattr(self.window, "resource_label"))
        self.window.asr_progress = None
        self.window.update_progress()
        self.assertEqual(self.window.progress.maximum(), 0)
        self.assertIn("cargando modelo", self.window.progress_label.text())
        self.window.pending.clear()
        self.window.running_id = None
        self.window.update_progress()
        self.assertFalse(self.window.progress_panel.isVisible())

    def test_progress_bar_spans_window_and_new_note_saves_current(self):
        self.window.pending.add(42)
        self.window.update_progress()
        self.application.processEvents()
        self.assertGreater(self.window.progress.width(), self.window.width() * 0.8)
        self.window.pending.clear()
        self.window.update_progress()
        self.window.note.setPlainText("Nota que conservar")
        self.window.new_capture()
        self.assertEqual(core.list_captures()[0]["transcript"], "Nota que conservar")
        self.assertEqual(self.window.note.toPlainText(), "")
        self.assertIsNone(self.window.capture_id)

    def test_close_with_backend_work_offers_force_in_dialog(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox

        self.window.busy = True
        seen = []

        def click_wait():
            dialogs = [
                w
                for w in self.application.topLevelWidgets()
                if isinstance(w, QMessageBox) and w.isVisible()
            ]
            self.assertEqual(len(dialogs), 1)
            buttons = {b.text(): b for b in dialogs[0].buttons()}
            self.assertIn("Forzar cierre", buttons)
            self.assertIn("Seguir esperando", buttons)
            seen.append(True)
            buttons["Seguir esperando"].click()

        QTimer.singleShot(30, click_wait)
        self.assertFalse(self.window.close())
        self.assertEqual(seen, [True])
        self.window.busy = False
