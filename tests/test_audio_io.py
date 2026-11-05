import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import av
import numpy as np

from backlogtalk import audio_io, core


class AudioInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.object(core, "AUDIO_DIR", self.root / "managed")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_discard_stops_stream_and_deletes_only_current_audio(self):
        recorder = audio_io.VoiceRecorder()
        recorder.path = self.root / "current.wav"
        recorder.path.write_bytes(b"temporary audio")
        other = self.root / "other.wav"
        other.write_bytes(b"keep")
        recorder.stream = MagicMock()
        stream = recorder.stream
        recorder.discard()
        stream.stop.assert_called_once()
        stream.close.assert_called_once()
        self.assertFalse((self.root / "current.wav").exists())
        self.assertEqual(other.read_bytes(), b"keep")
        self.assertIsNone(recorder.path)
        self.assertIsNone(recorder.stream)

    def test_discard_preserves_audio_if_stream_cannot_stop(self):
        recorder = audio_io.VoiceRecorder()
        recorder.path = self.root / "partial.wav"
        recorder.path.write_bytes(b"audio")
        recorder.stream = MagicMock()
        recorder.stream.stop.side_effect = RuntimeError("stop failed")
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            recorder.discard()
        self.assertTrue(recorder.path.exists())

    def test_discard_closes_writer_and_keeps_partial_when_inactive_stream_stop_fails(self):
        recorder = audio_io.VoiceRecorder()
        path = self.root / "partial.wav"
        path.write_bytes(b"audio")
        recorder.path = path
        recorder.stream = MagicMock()
        recorder.stream.active = False
        recorder.stream.stop.side_effect = RuntimeError("stop failed")
        recorder.writer = MagicMock()
        writer = recorder.writer
        with self.assertRaisesRegex(RuntimeError, "audio parcial") as error:
            recorder.discard()
        self.assertIn(str(path), str(error.exception))
        writer.close.assert_called_once()
        self.assertIsNone(recorder.writer)
        self.assertIsNone(recorder.stream)
        self.assertTrue(path.exists())

    def test_discard_reports_path_when_writer_close_fails(self):
        recorder = audio_io.VoiceRecorder()
        recorder.path = self.root / "partial.wav"
        recorder.path.write_bytes(b"audio")
        recorder.stream = MagicMock()
        recorder.writer = MagicMock()
        recorder.writer.close.side_effect = OSError("disk error")
        with self.assertRaisesRegex(RuntimeError, "partial.wav"):
            recorder.discard()
        self.assertTrue(recorder.path.exists())
        self.assertIsNotNone(recorder.writer)

    def test_meeting_discard_can_retry_only_the_source_that_failed(self):
        recorder = audio_io.MeetingRecorder()
        recorder.microphone = MagicMock()
        recorder.system = MagicMock()
        recorder.system.discard.side_effect = [RuntimeError("busy"), None]
        with self.assertRaisesRegex(RuntimeError, "busy"):
            recorder.discard()
        recorder.discard()
        recorder.microphone.discard.assert_called_once()
        self.assertEqual(recorder.system.discard.call_count, 2)

    def test_decode_audio_produces_float32_for_whisper(self):
        from backlogtalk.media_safety import decode_audio

        source = self.root / "decoder.wav"
        with wave.open(str(source), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(np.zeros(1600, dtype="<i2").tobytes())

        samples = decode_audio(source, lambda: None)
        self.assertEqual(len(samples), 1600)
        self.assertEqual(samples.dtype, np.float32)
        self.assertTrue(np.all(samples == 0))

    def test_decode_audio_resamples_stereo_and_checks_cancellation(self):
        from backlogtalk.media_safety import decode_audio

        source = self.root / "stereo.wav"
        samples = (np.sin(np.arange(48000) * 2 * np.pi * 330 / 48000) * 12000).astype("<i2")
        stereo = np.stack((samples, -samples // 2), axis=1)
        with wave.open(str(source), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(stereo.tobytes())
        decoded = decode_audio(source, lambda: None)
        self.assertEqual(len(decoded), 16000)
        self.assertEqual(decoded.dtype, np.float32)
        self.assertGreater(float(np.max(np.abs(decoded))), 0.01)
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise core.TranscriptionCancelled()

        with self.assertRaises(core.TranscriptionCancelled):
            decode_audio(source, cancel)
        self.assertTrue(source.is_file())

    def test_decode_audio_ignores_invalid_text_metadata(self):
        import struct

        from backlogtalk.media_safety import decode_audio

        source = self.root / "metadata.wav"
        with wave.open(str(source), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(bytes(3200))
        data = source.read_bytes()
        chunk = (
            b"LIST" + struct.pack("<I", 16) + b"INFOINAM" + struct.pack("<I", 4) + b"\xff\xfe\0\0"
        )
        source.write_bytes(
            data[:4] + struct.pack("<I", len(data) - 8 + len(chunk)) + data[8:] + chunk
        )
        self.assertEqual(len(decode_audio(source, lambda: None)), 1600)

    def test_import_wav_creates_managed_mono_pcm_and_keeps_original(self):
        source = self.root / "input.wav"
        with wave.open(str(source), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes((np.sin(np.arange(16000) * 0.1) * 5000).astype("<i2").tobytes())
        result = audio_io.import_media(source)
        self.assertTrue(source.exists())
        self.assertTrue(result.is_file())
        with wave.open(str(result), "rb") as inp:
            self.assertEqual(
                (inp.getnchannels(), inp.getframerate(), inp.getsampwidth()), (1, 16000, 2)
            )
            self.assertGreater(inp.getnframes(), 15000)

    def test_import_video_extracts_audio_only(self):
        import av

        source = self.root / "sample.mp4"
        with av.open(str(source), mode="w") as container:
            video = container.add_stream("mpeg4", rate=5)
            video.width = 64
            video.height = 64
            video.pix_fmt = "yuv420p"
            audio = container.add_stream("aac", rate=16000)
            audio.layout = "mono"
            image = av.VideoFrame.from_ndarray(
                np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24"
            )
            for packet in video.encode(image):
                container.mux(packet)
            samples = (np.sin(np.arange(16000) * 0.1) * 0.1).astype(np.float32).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(samples, format="flt", layout="mono")
            frame.sample_rate = 16000
            for packet in audio.encode(frame):
                container.mux(packet)
            for packet in video.encode(None):
                container.mux(packet)
            for packet in audio.encode(None):
                container.mux(packet)
        result = audio_io.import_media(source)
        with wave.open(str(result), "rb") as inp:
            self.assertGreater(inp.getnframes(), 15000)
        self.assertTrue(source.exists())

    def test_video_without_audio_reports_error_and_leaves_no_partial_file(self):
        import av

        source = self.root / "silent.mp4"
        with av.open(str(source), mode="w") as container:
            stream = container.add_stream("mpeg4", rate=5)
            stream.width = 64
            stream.height = 64
            stream.pix_fmt = "yuv420p"
            frame = av.VideoFrame.from_ndarray(
                np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24"
            )
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        with self.assertRaisesRegex(ValueError, "no contiene una pista de audio"):
            audio_io.import_media(source)
        self.assertEqual(list(core.AUDIO_DIR.iterdir()), [])
        self.assertTrue(source.exists())

    def test_invalid_input_leaves_no_partial_managed_file(self):
        source = self.root / "bad.mp4"
        source.write_bytes(b"not audio")
        with self.assertRaises(av.error.InvalidDataError):
            audio_io.import_media(source)
        self.assertEqual(list(core.AUDIO_DIR.iterdir()), [])
        self.assertTrue(source.exists())

    def test_interrupted_import_cleans_only_unreferenced_generated_files(self):
        token = "a" * 32
        core.private_directory(core.AUDIO_DIR)
        partial = core.AUDIO_DIR / f".import-{token}.partial"
        output = core.AUDIO_DIR / f"import-{token}.wav"
        with (
            patch.object(core, "APP_HOME", self.root),
            patch.object(core, "DB_FILE", self.root / "test.sqlite3"),
        ):
            core.db_connect().close()
            partial.write_bytes(b"partial")
            output.write_bytes(b"complete")
            capture = core.add_capture("DEMO", None, audio=str(output), source="file")
            audio_io.cleanup_interrupted_import(token)
            self.assertFalse(partial.exists())
            self.assertTrue(output.exists())
            core.delete_capture(capture)
            partial.write_bytes(b"partial")
            output.write_bytes(b"unreferenced")
            audio_io.cleanup_interrupted_import(token)
            self.assertFalse(partial.exists())
            self.assertFalse(output.exists())

    def test_microphone_stream_is_written_to_wav_incrementally(self):
        class InputStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.active = False

            def start(self):
                self.active = True
                self.callback(np.full((1600, 1), 0.25, dtype=np.float32), 1600, None, None)

            def stop(self):
                self.active = False

            def close(self):
                pass

        sd = SimpleNamespace(InputStream=InputStream, CallbackAbort=RuntimeError)
        with patch.dict("sys.modules", {"sounddevice": sd}):
            recorder = audio_io.VoiceRecorder()
            recorder.start()
            recorder.write_queue.join()
            self.assertTrue(recorder.stream.active)
            self.assertEqual(recorder.frames_written, 1600)
            recorder.writer._file.flush()
            with wave.open(str(recorder.path), "rb") as inp:
                self.assertEqual(inp.getnframes(), 1600)
            saved = recorder.stop_to_file()
            self.assertIsNone(recorder.stream)
        with wave.open(str(saved), "rb") as inp:
            values = np.frombuffer(inp.readframes(inp.getnframes()), dtype="<i2")
        self.assertGreater(int(values.max()), 8000)

    def test_input_overflow_is_detected(self):
        class InputStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.active = False

            def start(self):
                self.active = True
                self.callback(
                    np.ones((20, 1), dtype=np.float32),
                    20,
                    None,
                    SimpleNamespace(input_overflow=True),
                )

            def stop(self):
                self.active = False

            def close(self):
                pass

        sd = SimpleNamespace(InputStream=InputStream, CallbackAbort=RuntimeError)
        with patch.dict("sys.modules", {"sounddevice": sd}):
            recorder = audio_io.VoiceRecorder()
            recorder.start()
            path = recorder.stop_to_file()
        self.assertTrue(recorder.overflowed)
        self.assertTrue(path.exists())

    def test_saturated_queue_preserves_all_previously_accepted_audio(self):
        import threading

        class Abort(Exception):
            pass

        class InputStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.active = False

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def close(self):
                self.active = False

        original = wave.open
        entered = threading.Event()
        release = threading.Event()

        class SlowWriter:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def writeframes(self, data):
                if not entered.is_set():
                    entered.set()
                    release.wait(5)
                return self.wrapped.writeframes(data)

        def open_wave(path, mode):
            return SlowWriter(original(path, mode))

        sd = SimpleNamespace(InputStream=InputStream, CallbackAbort=Abort)
        with (
            patch.dict("sys.modules", {"sounddevice": sd}),
            patch.object(audio_io.wave, "open", side_effect=open_wave),
        ):
            recorder = audio_io.VoiceRecorder()
            recorder.start()
            path = recorder.path
            frame = np.full((160, 1), 0.1, dtype=np.float32)
            accepted = 1
            try:
                recorder.stream.callback(frame, 160, None, None)
                self.assertTrue(entered.wait(2))
                for _ in range(65):
                    try:
                        recorder.stream.callback(frame, 160, None, None)
                        accepted += 1
                    except Abort:
                        break
                self.assertEqual(accepted, 65)
            finally:
                release.set()
            with self.assertRaises(RuntimeError):
                recorder.stop_to_file()
            self.assertEqual(recorder.frames_written, accepted * 160)
        with wave.open(str(path), "rb") as saved:
            self.assertEqual(saved.getnframes(), accepted * 160)

    def test_abrupt_process_exit_leaves_readable_partial_wave(self):
        import os
        import subprocess
        import sys

        script = """import os, sys, types\nfrom pathlib import Path\nimport numpy as np\nfrom backlogtalk import core, audio_io\nclass Stream:\n def __init__(self, **kw): self.callback=kw["callback"]; self.active=True\n def start(self): pass\n def stop(self): self.active=False\n def close(self): pass\nsys.modules["sounddevice"]=types.SimpleNamespace(InputStream=Stream,CallbackAbort=RuntimeError)\ncore.AUDIO_DIR=Path(sys.argv[1]); r=audio_io.VoiceRecorder(); r.start()\nfor _ in range(10): r.stream.callback(np.zeros((1600,1),dtype=np.float32),1600,None,None); r.write_queue.join()\nos._exit(0)"""
        subprocess.run(
            [sys.executable, "-c", script, str(core.AUDIO_DIR)],
            cwd=str(core.ROOT),
            check=True,
            timeout=30,
            env=os.environ.copy(),
        )
        files = list(core.AUDIO_DIR.glob("*.wav"))
        self.assertEqual(len(files), 1)
        with wave.open(str(files[0]), "rb") as partial:
            self.assertEqual(partial.getnframes(), 16000)

    def test_stop_error_preserves_valid_partial_audio_if_stream_is_inactive(self):
        class InputStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.active = False

            def start(self):
                self.active = True
                self.callback(np.full((1600, 1), 0.25, dtype=np.float32), 1600, None, None)

            def stop(self):
                self.active = False
                raise RuntimeError("fuente desconectada")

            def close(self):
                pass

        with patch.dict(
            "sys.modules",
            {"sounddevice": SimpleNamespace(InputStream=InputStream, CallbackAbort=RuntimeError)},
        ):
            recorder = audio_io.VoiceRecorder()
            recorder.start()
            path = recorder.path
            with self.assertRaisesRegex(RuntimeError, "audio parcial"):
                recorder.stop_to_file()
        self.assertIsNone(recorder.stream)
        with wave.open(str(path), "rb") as inp:
            self.assertEqual(inp.getnframes(), 1600)

    def test_failed_device_start_removes_empty_file(self):
        class InputStream:
            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError("dispositivo ocupado")

            def close(self):
                pass

        with patch.dict(
            "sys.modules",
            {"sounddevice": SimpleNamespace(InputStream=InputStream, CallbackAbort=RuntimeError)},
        ):
            recorder = audio_io.VoiceRecorder()
            with self.assertRaises(RuntimeError):
                recorder.start()
        self.assertEqual(list(core.AUDIO_DIR.iterdir()), [])

    def test_output_monitor_matches_default_sink_and_never_uses_mic(self):
        devices = [
            {"name": "speaker.monitor", "max_input_channels": 2},
            {"name": "microphone", "max_input_channels": 1},
        ]
        sd = SimpleNamespace(query_devices=lambda: devices)
        result = SimpleNamespace(stdout='  * node.name = "speaker"\\n')
        with (
            patch.object(audio_io.sys, "platform", "linux"),
            patch.dict("sys.modules", {"sounddevice": sd}),
            patch.object(audio_io.subprocess, "run", return_value=result),
        ):
            self.assertEqual(audio_io.system_output_device(), 0)
            result.stdout = '  * node.name = "other"\\n'
            with self.assertRaises(RuntimeError):
                audio_io.system_output_device()

    def test_failure_to_start_microphone_stops_system_capture(self):
        class Track:
            def __init__(self, fail=False):
                self.fail = fail
                self.started = False
                self.stopped = False
                self.stream = None
                self.writer = None
                self.path = None

            def start(self, device):
                if self.fail:
                    raise RuntimeError("micrófono ocupado")
                self.started = True

            def stop_to_file(self):
                self.stopped = True
                return self_path

        self_path = self.root / "partial.wav"
        self_path.write_bytes(b"audio")
        microphone = Track(fail=True)
        system = Track()
        with patch.object(audio_io, "VoiceRecorder", side_effect=[microphone, system]):
            recorder = audio_io.MeetingRecorder()
            with self.assertRaisesRegex(RuntimeError, "micrófono ocupado"):
                recorder.start(1, 2)
        self.assertTrue(system.started)
        self.assertTrue(system.stopped)

    def test_failed_meeting_start_drains_writer_when_system_stop_fails(self):
        microphone = MagicMock()
        microphone.start.side_effect = RuntimeError("micrófono ocupado")
        system = audio_io.VoiceRecorder()
        system.path = self.root / "partial.wav"
        system.path.write_bytes(b"audio")
        system.stream = MagicMock()
        system.writer = MagicMock()
        system.write_queue = MagicMock()
        system.write_thread = MagicMock()
        queue, thread, writer, stream = (
            system.write_queue,
            system.write_thread,
            system.writer,
            system.stream,
        )
        with (
            patch.object(system, "start"),
            patch.object(system, "stop_to_file", side_effect=RuntimeError("detención fallida")),
            patch.object(audio_io, "VoiceRecorder", side_effect=[microphone, system]),
        ):
            recorder = audio_io.MeetingRecorder()
            with self.assertRaisesRegex(RuntimeError, "micrófono ocupado"):
                recorder.start(1, 2)
        stream.abort.assert_called_once()
        stream.close.assert_called_once()
        queue.put.assert_called_once_with(None)
        thread.join.assert_called_once()
        writer.close.assert_called_once()
        self.assertIsNone(system.write_thread)
        self.assertTrue(system.path.exists())

    def test_mix_preserves_both_sources_and_applies_offsets(self):
        core.AUDIO_DIR.mkdir()
        mic = core.AUDIO_DIR / "mic.wav"
        output = core.AUDIO_DIR / "system.wav"
        for path, samples in (
            (mic, np.full(10, 1000, dtype="<i2")),
            (output, np.full(6, 2000, dtype="<i2")),
        ):
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(samples.tobytes())
        job = audio_io.create_mix_job((mic, output, 2, 0), "DEMO", None)
        restored = audio_io.read_mix_job(job.manifest)
        self.assertEqual(job, restored)
        result = audio_io.mix_meeting(job)
        with wave.open(str(result), "rb") as inp:
            values = np.frombuffer(inp.readframes(inp.getnframes()), dtype="<i2")
        np.testing.assert_array_equal(
            values, [1000, 1000, 1500, 1500, 1500, 1500, 500, 500, 500, 500, 500, 500]
        )
        self.assertTrue(mic.exists())
        self.assertTrue(output.exists())
        self.assertTrue(job.manifest.exists())
        audio_io.finish_mix(job)
        self.assertTrue(result.exists())
        self.assertFalse(mic.exists())
        self.assertFalse(output.exists())
        self.assertFalse(job.manifest.exists())

    def test_failed_mix_keeps_both_originals_and_manifest(self):
        core.AUDIO_DIR.mkdir()
        mic = core.AUDIO_DIR / "mic.wav"
        output = core.AUDIO_DIR / "system.wav"
        mic.write_bytes(b"invalid")
        output.write_bytes(b"invalid")
        job = audio_io.create_mix_job((mic, output, 0, 0), "DEMO", None)
        with self.assertRaises((wave.Error, EOFError)):
            audio_io.mix_meeting(job)
        self.assertTrue(mic.exists())
        self.assertTrue(output.exists())
        self.assertTrue(job.manifest.exists())
        self.assertFalse(job.output.exists())

    def test_non_linux_output_does_not_capture_mic(self):
        with patch.object(audio_io.sys, "platform", "win32"):
            with self.assertRaises(RuntimeError):
                audio_io.system_output_device()


if __name__ == "__main__":
    unittest.main()
