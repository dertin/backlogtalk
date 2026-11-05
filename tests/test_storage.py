import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backlogtalk import storage


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "old-data"
        self.config = self.root / "old-config"
        self.data.mkdir()
        self.config.mkdir()
        (self.data / "audio").mkdir()
        self.audio = self.data / "audio" / "note.wav"
        self.audio.write_bytes(b"audio-original")
        (self.config / "config.json").write_text('{"whisper_model":"small"}', encoding="utf-8")
        (self.config / "SKILL.md").write_text("Instrucciones personales", encoding="utf-8")
        with closing(sqlite3.connect(self.data / "inbox.sqlite3")) as db, db:
            db.execute(
                "CREATE TABLE captures (id INTEGER PRIMARY KEY, audio_file TEXT, transcript TEXT)"
            )
            db.execute("INSERT INTO captures VALUES (1, ?, ?)", (str(self.audio), "texto"))
        self.destination = self.root / "destination"
        self.destination.mkdir()
        self.pointer = self.config / storage.LOCATION_NAME

    def test_copy_relinks_audio_and_keeps_originals(self):
        result = storage.relocate(self.data, self.config, self.pointer, self.destination)
        self.assertEqual(
            storage.configured_paths(self.data, self.config), (result / "data", result / "config")
        )
        self.assertEqual((result / "data/audio/note.wav").read_bytes(), b"audio-original")
        self.assertEqual((result / "config/SKILL.md").read_text(), "Instrucciones personales")
        with closing(sqlite3.connect(result / "data/inbox.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT audio_file, transcript FROM captures").fetchone(),
                (str(result / "data/audio/note.wav"), "texto"),
            )
        with closing(sqlite3.connect(self.data / "inbox.sqlite3")) as db, db:
            self.assertEqual(
                db.execute("SELECT audio_file FROM captures").fetchone()[0], str(self.audio)
            )
        self.assertTrue(self.audio.exists())
        self.assertTrue((self.config / "SKILL.md").exists())

    def test_existing_destination_never_overwritten(self):
        final = self.destination / "BacklogTalk-data"
        final.mkdir()
        (final / "existing").write_text("keep")
        with self.assertRaises(ValueError):
            storage.relocate(self.data, self.config, self.pointer, self.destination)
        self.assertEqual((final / "existing").read_text(), "keep")
        self.assertFalse(self.pointer.exists())

    def test_missing_audio_aborts_without_switch(self):
        self.audio.unlink()
        with self.assertRaises(FileNotFoundError):
            storage.relocate(self.data, self.config, self.pointer, self.destination)
        self.assertFalse(self.pointer.exists())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_pointer_write_failure_preserves_source_and_cleans_copy(self):
        with patch.object(storage.os, "replace", side_effect=OSError("falló")):
            with self.assertRaises(OSError):
                storage.relocate(self.data, self.config, self.pointer, self.destination)
        self.assertFalse(self.pointer.exists())
        self.assertTrue(self.audio.exists())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_missing_active_location_is_not_silently_replaced(self):
        self.pointer.write_text(json.dumps({"root": str(self.root / "missing")}))
        with self.assertRaises(RuntimeError):
            storage.configured_paths(self.data, self.config)

    def test_same_directory_for_data_and_config(self):
        (self.data / "config.json").write_text("{}")
        (self.data / "SKILL.md").write_text("reglas")
        pointer = self.data / storage.LOCATION_NAME
        result = storage.relocate(self.data, self.data, pointer, self.destination)
        self.assertEqual((result / "config/SKILL.md").read_text(), "reglas")
        self.assertFalse((result / "data/SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
