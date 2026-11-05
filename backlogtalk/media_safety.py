"""Require media decoding libraries with fixes for known native crashes."""

import io
import re


def load_safe_media_backend():
    import av

    version = getattr(av, "__version__", "")
    release = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if not release or not ((19, 0, 2) <= tuple(map(int, release.groups())) < (20, 0, 0)):
        raise RuntimeError(
            "La versión de PyAV no es segura para procesar audio. Instalá una versión de PyAV desde 19.0.2 hasta antes de 20."
        )
    libraries = getattr(av, "library_versions", {})
    current = libraries.get("libavformat") if isinstance(libraries, dict) else None
    if (
        not isinstance(current, tuple)
        or len(current) != 3
        or not all(
            isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in current
        )
    ):
        raise RuntimeError("No se pudo verificar la versión de FFmpeg usada por PyAV.")
    major, minor, micro = current
    safe = (
        (major == 61 and current >= (61, 7, 102))
        or (major == 62 and ((minor == 3 and micro >= 102) or current >= (62, 6, 103)))
        or (major == 63 and current >= (63, 1, 100))
    )
    if not safe:
        raise RuntimeError(
            "La versión de FFmpeg usada por PyAV no es compatible con la protección de audio. "
            "Actualizá PyAV o reconstruílo con una versión corregida de FFmpeg."
        )
    return av


def decode_audio(audio_file, check_cancel):
    """Decode to 16 kHz mono float32 for Whisper, checking cancellation per frame."""
    import numpy as np

    av = load_safe_media_backend()
    buffer = io.BytesIO()
    with av.open(str(audio_file), mode="r") as container:
        if not container.streams.audio:
            raise ValueError("El archivo no contiene una pista de audio.")
        stream = container.streams.audio[0]
        if stream.codec_context is None:
            raise ValueError("La pista de audio no tiene un decodificador válido.")
        fifo = av.audio.fifo.AudioFifo()
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

        def write_resampled(frame):
            for converted in resampler.resample(frame):
                buffer.write(converted.to_ndarray().tobytes())

        frames = iter(container.decode(stream))
        while True:
            check_cancel()
            try:
                frame = next(frames)
            except StopIteration:
                break
            except av.error.InvalidDataError:
                continue
            frame.pts = None
            fifo.write(frame)
            if fifo.samples >= 500000:
                write_resampled(fifo.read())
        if fifo.samples:
            write_resampled(fifo.read())
        write_resampled(None)
    check_cancel()
    if not buffer.tell():
        raise ValueError("La pista de audio está vacía.")
    samples = np.frombuffer(buffer.getbuffer(), dtype="<i2").astype(np.float32)
    samples /= 32768.0
    return samples
