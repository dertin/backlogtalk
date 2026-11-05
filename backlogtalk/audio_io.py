"""Capture and import audio into the managed local inbox."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from backlogtalk import core
from backlogtalk.media_safety import load_safe_media_backend


def system_output_device() -> int:
    """Find the monitor of the current Linux output; never fall back to a microphone."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError(
            "La captura de salida del sistema todavía no está disponible en este sistema. Podés importar un archivo de audio o video."
        )
    import sounddevice as sd

    monitors = [
        (i, d["name"])
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0 and d["name"].endswith(".monitor")
    ]
    try:
        result = subprocess.run(
            ["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
        found = re.search(r'^\s*\*?\s*node\.name\s*=\s*"([^"]+)"', result.stdout, re.MULTILINE)
    except (OSError, subprocess.SubprocessError):
        found = None
    if found:
        expected = found.group(1) + ".monitor"
        for index, name in monitors:
            if name == expected:
                return index
        raise RuntimeError(
            "No se encontró el monitor de la salida de audio predeterminada. Revisá PipeWire y PortAudio."
        )
    if len(monitors) == 1:
        return monitors[0][0]
    raise RuntimeError(
        "No se pudo identificar la salida predeterminada. Se necesita PipeWire con wpctl y una fuente monitor."
    )


class VoiceRecorder:
    def __init__(self):
        self.stream = None
        self.level = 0.0
        self.writer = None
        self.path = None
        self.frames_written = 0
        self.write_error = None
        self.first_frame_at = None
        self.overflowed = False
        self.write_queue = None
        self.write_thread = None

    def _finish_writer(self):
        if self.write_queue is not None:
            self.write_queue.put(None)
            self.write_thread.join()
            self.write_queue = None
            self.write_thread = None
        if self.writer is not None:
            self.writer.close()
            self.writer = None

    def start(self, device=None):
        import numpy as np
        import sounddevice as sd

        core.private_directory(core.AUDIO_DIR)
        self.path = core.AUDIO_DIR / f"{uuid.uuid4().hex}.wav"
        self.frames_written = 0
        self.write_error = None
        self.level = 0.0
        self.first_frame_at = None
        self.overflowed = False
        self.path.touch(mode=0o600, exist_ok=False)
        self.writer = wave.open(str(self.path), "wb")
        self.writer.setnchannels(1)
        self.writer.setsampwidth(2)
        self.writer.setframerate(16000)
        self.write_queue = queue.Queue(maxsize=64)

        def write_audio():
            disk_error = None
            while True:
                item = self.write_queue.get()
                try:
                    if item is None:
                        return
                    if disk_error is None:
                        try:
                            pcm, frames = item
                            self.writer.writeframes(pcm)
                            self.writer._file.flush()
                            self.frames_written += frames
                        except Exception as error:
                            disk_error = error
                            self.write_error = error
                finally:
                    self.write_queue.task_done()

        self.write_thread = threading.Thread(
            target=write_audio, name="backlogtalk-audio-writer", daemon=True
        )
        self.write_thread.start()

        def callback(indata, frames, time_info, status):
            try:
                if status and getattr(status, "input_overflow", False):
                    self.overflowed = True
                if self.write_error is not None:
                    raise self.write_error
                if self.first_frame_at is None:
                    self.first_frame_at = time.monotonic()
                self.level = float(abs(indata).max())
                pcm = (np.clip(indata, -1, 1) * 32767).astype("<i2")
                try:
                    self.write_queue.put_nowait((pcm.tobytes(), frames))
                except queue.Full as error:
                    raise RuntimeError(
                        "La escritura de audio no pudo seguir el ritmo de la grabación."
                    ) from error
            except Exception as error:
                self.write_error = error
                raise sd.CallbackAbort from error

        try:
            self.stream = sd.InputStream(
                device=device, samplerate=16000, channels=1, dtype="float32", callback=callback
            )
            self.stream.start()
        except Exception:
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            self._finish_writer()
            self.path.unlink(missing_ok=True)
            raise

    def _stop_stream(self):
        if self.stream is None:
            raise RuntimeError("No se estaba grabando")
        try:
            self.stream.stop()
            self.stream.close()
        except Exception as error:
            try:
                active = self.stream.active
            except Exception:
                active = True
            if active:
                raise
            path = self.path
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None
            try:
                self._finish_writer()
            except Exception as close_error:
                raise RuntimeError(
                    f"No se pudo cerrar el audio parcial: {path}: {close_error}"
                ) from close_error
            self.path = None
            raise RuntimeError(
                f"No se pudo cerrar la fuente; audio parcial: {path}: {error}"
            ) from error
        self.stream = None
        try:
            self._finish_writer()
        except Exception as error:
            raise RuntimeError(
                f"No se pudo cerrar el audio parcial: {self.path}: {error}"
            ) from error

    def discard(self):
        self._stop_stream()
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None

    def stop_to_file(self) -> Path:
        self._stop_stream()
        path = self.path
        self.path = None
        if self.write_error:
            raise RuntimeError(
                f"Error al guardar audio; archivo parcial: {path}: {self.write_error}"
            )
        if not self.frames_written:
            path.unlink(missing_ok=True)
            raise RuntimeError("No se recibió audio de la fuente seleccionada")
        return path


@dataclass(frozen=True)
class MixJob:
    manifest: Path
    microphone: Path
    system: Path
    output: Path
    mic_delay: int
    system_delay: int
    project: str
    epic: str | None


class MeetingRecorder:
    """Record microphone and output independently, then mix them after stopping."""

    def __init__(self):
        self.microphone = VoiceRecorder()
        self.system = VoiceRecorder()
        self.saved = {}
        self.discarded = set()

    @property
    def level(self):
        return max(self.microphone.level, self.system.level)

    @property
    def active(self):
        for recorder in (self.microphone, self.system):
            if recorder.stream is not None:
                try:
                    if recorder.stream.active:
                        return True
                except Exception:
                    return True
        return False

    def start(self, microphone_device, system_device):
        self.system.start(system_device)
        try:
            self.microphone.start(microphone_device)
        except Exception as error:
            try:
                partial = self.system.stop_to_file()
                detail = f" El audio parcial se conservó en {partial}."
            except Exception as stop_error:
                partial = self.system.path
                if self.system.stream is not None:
                    try:
                        self.system.stream.abort()
                        self.system.stream.close()
                        self.system.stream = None
                    except Exception as close_error:
                        raise RuntimeError(
                            f"No se pudo abrir el micrófono ni detener la salida: {close_error}. "
                            f"Audio parcial: {partial}."
                        ) from error
                try:
                    self.system._finish_writer()
                except Exception as close_error:
                    raise RuntimeError(
                        f"No se pudo cerrar la salida: {close_error}. Audio parcial: {partial}."
                    ) from stop_error
                detail = f" Audio parcial: {partial}." if partial and partial.exists() else ""
            raise RuntimeError(f"No se pudo abrir el micrófono: {error}.{detail}") from error

    def discard(self):
        errors = []
        for name, recorder in (("microphone", self.microphone), ("system", self.system)):
            if name in self.discarded:
                continue
            try:
                recorder.discard()
                self.discarded.add(name)
            except Exception as error:
                errors.append(f"{name}: {error}")
        if errors:
            raise RuntimeError("No se pudieron descartar todas las pistas: " + "; ".join(errors))

    def stop_sources(self):
        errors = []
        for name, recorder in (("microphone", self.microphone), ("system", self.system)):
            if name in self.saved:
                continue
            try:
                self.saved[name] = recorder.stop_to_file()
            except Exception as error:
                errors.append(f"{name}: {error}")
        if errors:
            kept = ", ".join(str(path) for path in self.saved.values())
            raise RuntimeError(
                "No se pudieron detener ambas fuentes: "
                + "; ".join(errors)
                + (f". Audio conservado: {kept}" if kept else "")
            )
        mic_time = self.microphone.first_frame_at
        system_time = self.system.first_frame_at
        if mic_time is None or system_time is None:
            raise RuntimeError("No se recibieron muestras de ambas fuentes.")
        origin = min(mic_time, system_time)
        return (
            self.saved["microphone"],
            self.saved["system"],
            max(0, round((mic_time - origin) * 16000)),
            max(0, round((system_time - origin) * 16000)),
        )


def create_mix_job(sources, project: str, epic: str | None) -> MixJob:
    mic, system, mic_delay, system_delay = sources
    directory = core.AUDIO_DIR
    if mic.parent != directory or system.parent != directory:
        raise ValueError("Las pistas deben estar en la carpeta de audio.")
    token = uuid.uuid4().hex
    manifest = directory / f"mix-{token}.json"
    output = directory / f"{token}.wav"
    payload = {
        "microphone": mic.name,
        "system": system.name,
        "output": output.name,
        "mic_delay": mic_delay,
        "system_delay": system_delay,
        "project": project,
        "epic": epic,
    }
    fd, temporary = tempfile.mkstemp(prefix=".mix-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, manifest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return read_mix_job(manifest)


def read_mix_job(manifest: Path) -> MixJob:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    directory = manifest.parent

    def managed(name):
        value = data[name]
        if (
            not isinstance(value, str)
            or not value
            or Path(value).name != value
            or not value.endswith(".wav")
        ):
            raise ValueError("Archivo de mezcla inválido")
        return directory / value

    delays = [data["mic_delay"], data["system_delay"]]
    if any(type(x) is not int or x < 0 or x > 16000 * 60 for x in delays):
        raise ValueError("Sincronización inválida")
    if not isinstance(data["project"], str) or not data["project"]:
        raise ValueError("Proyecto inválido")
    if data["epic"] is not None and not isinstance(data["epic"], str):
        raise ValueError("Épica inválida")
    microphone, system, output = managed("microphone"), managed("system"), managed("output")
    if len({microphone, system, output}) != 3 or any(
        path.is_symlink() for path in (microphone, system, output)
    ):
        raise ValueError("Las pistas y el resultado deben ser archivos distintos y locales")
    return MixJob(manifest, microphone, system, output, *delays, data["project"], data["epic"])


def mix_meeting(job: MixJob, progress=None) -> Path:
    """Mix aligned streams in small chunks; leave originals and manifest on failure."""
    import numpy as np

    if job.output.is_file():
        with wave.open(str(job.output), "rb") as existing:
            if (
                existing.getnchannels() == 1
                and existing.getframerate() == 16000
                and existing.getnframes() > 0
            ):
                return job.output
        raise ValueError(f"El audio mezclado existente no es válido: {job.output}")
    temp = job.output.with_suffix(".mixing")
    try:
        with (
            wave.open(str(job.microphone), "rb") as mic,
            wave.open(str(job.system), "rb") as system,
            wave.open(str(temp), "wb") as out,
        ):
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            for stream in (mic, system):
                if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (
                    1,
                    2,
                    16000,
                ):
                    raise ValueError("Formato de pista incompatible")
            total = max(job.mic_delay + mic.getnframes(), job.system_delay + system.getnframes())
            position = 0
            started = time.monotonic()
            last_update = 0.0
            if progress and total:
                progress(0.0, total / 16000, 0.0)
            while position < total:
                length = min(8192, total - position)
                result = np.zeros(length, dtype=np.int32)
                for stream, delay in ((mic, job.mic_delay), (system, job.system_delay)):
                    offset = max(0, min(length, delay - position))
                    samples = np.frombuffer(stream.readframes(length - offset), dtype="<i2").astype(
                        np.int32
                    )
                    result[offset : offset + len(samples)] += samples
                out.writeframesraw((result // 2).astype("<i2").tobytes())
                position += length
                elapsed = time.monotonic() - started
                if progress and (elapsed - last_update >= 0.3 or position == total):
                    progress(position / 16000, total / 16000, elapsed)
                    last_update = elapsed
        os.replace(temp, job.output)
        return job.output
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def finish_mix(job: MixJob) -> None:
    for path in (job.microphone, job.system):
        path.unlink(missing_ok=True)
    job.manifest.unlink(missing_ok=True)


def import_media(source: Path, progress=None, operation_id: str | None = None) -> Path:
    """Extract one audio stream incrementally to a managed mono PCM WAV file."""
    av = load_safe_media_backend()

    core.private_directory(core.AUDIO_DIR)
    token = operation_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Identificador de importación inválido")
    path = core.AUDIO_DIR / f"import-{token}.wav"
    temporary = core.AUDIO_DIR / f".import-{token}.partial"
    try:
        temporary.touch(mode=0o600, exist_ok=False)
        with av.open(str(source)) as container, wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            if not container.streams.audio:
                raise ValueError("El archivo no contiene una pista de audio.")
            stream = container.streams.audio[0]
            if stream.codec_context is None:
                raise ValueError("La pista de audio no tiene un decodificador válido.")
            total = (
                float(stream.duration * stream.time_base)
                if stream.duration is not None and stream.time_base
                else (float(container.duration / av.time_base) if container.duration else 0.0)
            )
            origin = (
                float(stream.start_time * stream.time_base)
                if stream.start_time is not None and stream.time_base
                else 0.0
            )
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            count = 0
            started = time.monotonic()
            last_update = 0.0
            if progress and total > 0:
                progress(0.0, total, 0.0)
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    output.writeframesraw(converted.to_ndarray().tobytes())
                    count += converted.samples
                elapsed = time.monotonic() - started
                if progress and total > 0 and elapsed - last_update >= 0.3:
                    position = (
                        max(0.0, float(frame.pts * frame.time_base) - origin)
                        if frame.pts is not None and frame.time_base
                        else count / 16000
                    )
                    progress(min(position, total), total, elapsed)
                    last_update = elapsed
            for converted in resampler.resample(None):
                output.writeframesraw(converted.to_ndarray().tobytes())
                count += converted.samples
            if not count:
                raise ValueError("La pista de audio está vacía.")
            if progress and total > 0:
                progress(total, total, time.monotonic() - started)
        os.replace(temporary, path)
        return path
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def cleanup_interrupted_import(operation_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("Identificador de importación inválido")
    temporary = core.AUDIO_DIR / f".import-{operation_id}.partial"
    output = core.AUDIO_DIR / f"import-{operation_id}.wav"
    if temporary.is_symlink() or output.is_symlink():
        raise ValueError("Archivo de importación inválido")
    temporary.unlink(missing_ok=True)
    with core.db_connect() as db:
        referenced = db.execute(
            "SELECT 1 FROM captures WHERE audio_file=? LIMIT 1", (str(output),)
        ).fetchone()
    if not referenced:
        output.unlink(missing_ok=True)
