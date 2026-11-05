import unittest
from pathlib import PurePosixPath
from unittest.mock import patch

from backlogtalk import shortcuts


class GlobalShortcutTests(unittest.TestCase):
    def test_reads_actual_bindings_for_this_installation_only(self):
        root = PurePosixPath("/tmp/backlogtalk")
        prefix = shortcuts._PREFIX
        paths = [prefix + "custom1/", prefix + "custom2/", prefix + "custom3/", prefix + "other/"]
        values = {
            (shortcuts._SCHEMA, "custom-keybindings"): paths,
            (
                shortcuts._CUSTOM + ":" + paths[0],
                "command",
            ): "/tmp/backlogtalk/scripts/run.sh --capture",
            (shortcuts._CUSTOM + ":" + paths[0], "binding"): "<Control><Alt>j",
            (
                shortcuts._CUSTOM + ":" + paths[1],
                "command",
            ): "/tmp/backlogtalk/scripts/run.sh --capture-system",
            (shortcuts._CUSTOM + ":" + paths[1], "binding"): "<Control><Alt>k",
            (
                shortcuts._CUSTOM + ":" + paths[2],
                "command",
            ): "/tmp/backlogtalk/scripts/run.sh --open-history",
            (shortcuts._CUSTOM + ":" + paths[2], "binding"): "<Control><Alt>b",
            (shortcuts._CUSTOM + ":" + paths[3], "command"): "/tmp/other/scripts/run.sh --capture",
            (shortcuts._CUSTOM + ":" + paths[3], "binding"): "<Control><Alt>x",
        }
        with (
            patch.object(shortcuts.sys, "platform", "linux"),
            patch.dict(shortcuts.os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}),
            patch.object(
                shortcuts, "_read_value", side_effect=lambda schema, key: values[schema, key]
            ),
        ):
            self.assertEqual(
                shortcuts.gnome_global_shortcuts(root),
                {
                    "mic": ["Ctrl+Alt+J"],
                    "meeting": ["Ctrl+Alt+K"],
                    "open": ["Ctrl+Alt+B"],
                },
            )

    def test_unavailable_system_is_not_reported_as_unassigned(self):
        with patch.object(shortcuts.sys, "platform", "win32"):
            self.assertIsNone(shortcuts.gnome_global_shortcuts(PurePosixPath("/tmp/backlogtalk")))

    def test_empty_and_unsupported_bindings_are_not_invented(self):
        self.assertEqual(shortcuts.format_binding(""), "")
        self.assertEqual(shortcuts.format_binding("<Unknown>b"), "")
        self.assertEqual(shortcuts.format_binding("<Super><Shift>f2"), "Super+Shift+f2")
