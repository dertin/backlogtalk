import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backlogtalk import audio_io, core, media_safety


class MediaSafetyTests(unittest.TestCase):
    def test_rejects_unsafe_and_unverifiable_library_versions(self):
        for pyav, library in (
            ("17.1.0", (62, 12, 102)),
            ("18.0.0", (62, 12, 102)),
            ("18.1.0", (62, 12, 102)),
            ("19.0.0", (62, 12, 102)),
            ("19.0.1", (62, 12, 102)),
            ("20.0.0", (63, 1, 102)),
            ("18.0.0rc1", (62, 12, 102)),
            ("19.0.2", (62, 3, 100)),
            ("19.0.2", (62, 6, 102)),
            ("19.0.2", (63, 1, 99)),
            ("19.0.2", None),
        ):
            with (
                self.subTest(pyav=pyav, library=library),
                patch.dict(
                    sys.modules,
                    {
                        "av": SimpleNamespace(
                            __version__=pyav, library_versions={"libavformat": library}
                        )
                    },
                ),
            ):
                with self.assertRaises(RuntimeError):
                    media_safety.load_safe_media_backend()

    def test_accepts_known_fixed_library_versions(self):
        for library in ((61, 7, 102), (62, 3, 102), (62, 6, 103), (62, 12, 102), (63, 1, 100)):
            with (
                self.subTest(library=library),
                patch.dict(
                    sys.modules,
                    {
                        "av": SimpleNamespace(
                            __version__="19.0.2", library_versions={"libavformat": library}
                        )
                    },
                ),
            ):
                self.assertEqual(
                    media_safety.load_safe_media_backend().library_versions["libavformat"], library
                )

    def test_import_refuses_unsafe_backend_before_opening_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"input")
            backend = MagicMock(
                __version__="18.1.0", library_versions={"libavformat": (62, 12, 102)}
            )
            with (
                patch.dict(sys.modules, {"av": backend}),
                patch.object(core, "AUDIO_DIR", Path(directory) / "output"),
            ):
                with self.assertRaisesRegex(RuntimeError, "PyAV"):
                    audio_io.import_media(source)
            backend.open.assert_not_called()
            self.assertEqual(source.read_bytes(), b"input")
            self.assertFalse((Path(directory) / "output").exists())

    def test_transcription_refuses_unsafe_backend_before_loading_model(self):
        backend = MagicMock(__version__="18.1.0", library_versions={"libavformat": (62, 12, 102)})
        with (
            patch.dict(sys.modules, {"av": backend}),
            patch.object(core, "_whisper_model") as model,
        ):
            with self.assertRaisesRegex(RuntimeError, "PyAV"):
                core.transcribe(Path("input.wav"), "small")
        model.assert_not_called()
        backend.open.assert_not_called()

    def test_import_rejects_audio_stream_without_decoder_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp4"
            source.write_bytes(b"input")
            container = MagicMock()
            container.streams.audio = [SimpleNamespace(codec_context=None)]
            backend = MagicMock()
            backend.open.return_value.__enter__.return_value = container
            with (
                patch.object(audio_io, "load_safe_media_backend", return_value=backend),
                patch.object(core, "AUDIO_DIR", root / "audio"),
            ):
                with self.assertRaisesRegex(ValueError, "decodificador"):
                    audio_io.import_media(source)
                self.assertEqual(list((root / "audio").iterdir()), [])
                container.decode.assert_not_called()
                self.assertEqual(source.read_bytes(), b"input")
