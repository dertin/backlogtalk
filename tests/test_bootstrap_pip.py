import hashlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import bootstrap_pip


class BootstrapPipTests(unittest.TestCase):
    def test_rejects_invalid_download_before_running_pip(self):
        with (
            patch.object(bootstrap_pip.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")),
            patch.object(bootstrap_pip.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "integridad"):
                bootstrap_pip.main()
        run.assert_not_called()

    def test_rejects_oversized_download_before_running_pip(self):
        with (
            patch.object(bootstrap_pip, "MAX_WHEEL_BYTES", 3),
            patch.object(bootstrap_pip.urllib.request, "urlopen", return_value=io.BytesIO(b"1234")),
            patch.object(bootstrap_pip.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "integridad"):
                bootstrap_pip.main()
        run.assert_not_called()

    def test_verified_wheel_is_used_in_isolated_offline_install(self):
        content = b"verified wheel"

        def install(command, *, check):
            self.assertTrue(check)
            self.assertEqual(command[1:3], ["-I", "-c"])
            self.assertIn("--isolated", command)
            self.assertIn("--no-index", command)
            self.assertEqual(Path(command[-1]).read_bytes(), content)
            self.assertEqual(command[4], command[-1])

        with (
            patch.object(bootstrap_pip.urllib.request, "urlopen", return_value=io.BytesIO(content)),
            patch.object(bootstrap_pip, "WHEEL_SHA256", hashlib.sha256(content).hexdigest()),
            patch.object(bootstrap_pip.subprocess, "run", side_effect=install) as run,
        ):
            bootstrap_pip.main()
        run.assert_called_once()
        self.assertFalse(Path(run.call_args.args[0][-1]).exists())

    def test_installer_failure_is_not_ignored(self):
        content = b"verified wheel"
        with (
            patch.object(bootstrap_pip.urllib.request, "urlopen", return_value=io.BytesIO(content)),
            patch.object(bootstrap_pip, "WHEEL_SHA256", hashlib.sha256(content).hexdigest()),
            patch.object(
                bootstrap_pip.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "pip"),
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap_pip.main()
