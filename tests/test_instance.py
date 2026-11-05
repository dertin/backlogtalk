import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backlogtalk.instance import Instance


def send_from_process(root, command, poll):
    script = (
        "import sys; from pathlib import Path; from backlogtalk.instance import Instance; "
        "instance = Instance(Path(sys.argv[1])); "
        "primary = instance.acquire_or_send(sys.argv[2]); instance.close(); "
        "sys.exit(1 if primary else 0)"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(root), command])
    deadline = time.monotonic() + 10
    try:
        while child.poll() is None and time.monotonic() < deadline:
            poll()
            time.sleep(0.01)
        if child.poll() is None:
            raise TimeoutError("La segunda instancia no terminó")
        if child.returncode:
            raise AssertionError(f"La segunda instancia falló: {child.returncode}")
        poll()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


class InstanceTests(unittest.TestCase):
    def test_system_capture_command_is_delivered_to_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Instance(Path(tmp))
            commands = []
            try:
                self.assertTrue(first.acquire_or_send("open"))
                send_from_process(
                    Path(tmp), "capture-system", lambda: commands.extend(first.receive())
                )
                self.assertEqual(commands, ["capture-system"])
            finally:
                first.close()

    def test_open_history_command_reaches_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Instance(Path(tmp))
            commands = []
            try:
                self.assertTrue(first.acquire_or_send("open"))
                send_from_process(
                    Path(tmp), "open-history", lambda: commands.extend(first.receive())
                )
                self.assertEqual(commands, ["open-history"])
            finally:
                first.close()

    def test_second_invocation_delivers_capture_to_primary(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"XDG_RUNTIME_DIR": directory, "TEMP": directory, "TMP": directory}
            ),
            patch("backlogtalk.instance.tempfile.gettempdir", return_value=directory),
        ):
            first = Instance(Path(directory) / "project")
            commands = []
            try:
                self.assertTrue(first.acquire_or_send("open"))
                send_from_process(
                    Path(directory) / "project", "capture", lambda: commands.extend(first.receive())
                )
                self.assertEqual(commands, ["capture"])
            finally:
                first.close()


@unittest.skipIf(sys.platform == "win32", "Simulates Windows file locking on Unix")
class WindowsInstanceSimulationTests(unittest.TestCase):
    def test_windows_branch_delivers_to_existing_instance(self):
        import errno
        import fcntl
        import types

        def lock(fd, _mode, _length):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OSError(errno.EACCES, "locked") from error

        fake_msvcrt = types.SimpleNamespace(LK_NBLCK=2, locking=lock)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("backlogtalk.instance.tempfile.gettempdir", return_value=directory),
            patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            patch("backlogtalk.instance.sys.platform", "win32"),
        ):
            first = Instance(Path(directory) / "project")
            second = Instance(Path(directory) / "project")
            try:
                self.assertTrue(first.acquire_or_send("open"))
                self.assertFalse(second.acquire_or_send("capture"))
                self.assertEqual(first.receive(), ["capture"])
            finally:
                second.close()
                first.close()

    def test_windows_accepts_immediately_written_command(self):
        import errno
        import fcntl
        import types

        def lock(fd, _mode, _length):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OSError(errno.EACCES, "locked") from error

        fake_msvcrt = types.SimpleNamespace(LK_NBLCK=2, locking=lock)
        client = MagicMock()
        client.waitForConnected.return_value = True
        client.write.return_value = 7
        client.bytesToWrite.return_value = 0
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("backlogtalk.instance.tempfile.gettempdir", return_value=directory),
            patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            patch("backlogtalk.instance.sys.platform", "win32"),
            patch("backlogtalk.instance.QLocalSocket", return_value=client),
        ):
            first = Instance(Path(directory) / "project")
            second = Instance(Path(directory) / "project")
            try:
                self.assertTrue(first.acquire_or_send("open"))
                self.assertFalse(second.acquire_or_send("capture"))
                client.waitForBytesWritten.assert_not_called()
                client.bytesToWrite.side_effect = [1, 0]
                client.waitForBytesWritten.return_value = False
                self.assertFalse(second.acquire_or_send("capture"))
                client.waitForBytesWritten.assert_called_once_with(500)
            finally:
                second.close()
                first.close()
