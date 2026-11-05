import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from backlogtalk import core


class PortabilityTests(unittest.TestCase):
    def test_window_exits_cleanly_in_a_separate_process(self):
        script = """
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from backlogtalk import qt_app

qt_app.shortcuts.gnome_global_shortcuts = lambda root: None
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
window = qt_app.VoiceWindow()
window.show()
QTimer.singleShot(100, window.request_exit)
app.exec()
window.transcription_thread.join(timeout=5)
assert window.closing and not window.transcription_thread.is_alive()
print('normal exit')
"""
        with tempfile.TemporaryDirectory() as root:
            environment = dict(os.environ)
            environment.update(
                HOME=root,
                USERPROFILE=root,
                LOCALAPPDATA=root,
                XDG_DATA_HOME=root,
                XDG_CONFIG_HOME=root,
                QT_QPA_PLATFORM="offscreen",
            )
            result = subprocess.run(
                [sys.executable, "-X", "faulthandler", "-c", script],
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("normal exit", result.stdout)

    def test_windows_data_paths_use_localappdata(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LOCALAPPDATA": root}):
            data, config = core.data_paths("win32")
            self.assertEqual(data, Path(root) / "BacklogTalk")
            self.assertEqual(config, data)

    def test_macos_data_paths_use_application_support(self):
        with patch.object(Path, "home", return_value=Path("/example/user")):
            data, config = core.data_paths("darwin")
        self.assertEqual(data, Path("/example/user/Library/Application Support/BacklogTalk"))
        self.assertEqual(config, data)

    def test_cursor_prompt_is_stdin_not_a_shell_argument(self):
        note = 'Revisar comillas "dobles", %PATH%, ! y & echo prueba'
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(core, "CONFIG_DIR", Path(root)),
            patch.object(core, "find_cursor", return_value="agent.cmd"),
            patch.object(
                core.subprocess, "run", return_value=CompletedProcess([], 0, '{"tickets":[]}', "")
            ) as run,
        ):
            self.assertEqual(core.draft_with_cursor(note), [])
        self.assertIn(note, run.call_args.kwargs["input"])
        self.assertNotIn(note, run.call_args.args[0])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")

    def test_cursor_model_is_explicit_and_rejects_shell_metacharacters(self):
        result = CompletedProcess([], 0, '{"tickets":[]}', "")
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(core, "CONFIG_DIR", Path(root)),
            patch.object(core, "find_cursor", return_value="agent.cmd"),
            patch.object(core.subprocess, "run", return_value=result) as run,
        ):
            core.draft_with_cursor("Nota", "composer-2.5")
            self.assertEqual(run.call_args.args[0][-2:], ["--model", "composer-2.5"])
            with self.assertRaises(ValueError):
                core.draft_with_cursor("Nota", "model & echo x")
            self.assertEqual(run.call_count, 1)

    def test_cursor_list_models_parses_ids(self):
        result = CompletedProcess(
            [],
            0,
            "Available models\n\nauto - Auto (default)\ncomposer-2.5 - Composer (current)\n",
            "",
        )
        with (
            patch.object(core, "find_cursor", return_value="agent"),
            patch.object(core.subprocess, "run", return_value=result),
        ):
            self.assertEqual(core.list_cursor_models(), (["auto", "composer-2.5"], "composer-2.5"))

    def test_windows_jira_requires_executable_not_batch_wrapper(self):
        with (
            patch.object(core.sys, "platform", "win32"),
            patch.object(core.shutil, "which", return_value=None) as which,
            patch.dict(os.environ, {"LOCALAPPDATA": ""}),
        ):
            with self.assertRaises(RuntimeError):
                core.find_twg()
        which.assert_called_once_with("twg.exe")
