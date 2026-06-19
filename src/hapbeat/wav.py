"""Read a WAV file's PCM16 bytes for clip streaming.

The device streams uncompressed 16-bit PCM (16 kHz expected, matching the
kit-tools normalization). This thin wrapper over the stdlib :mod:`wave` module
returns the raw interleaved PCM16-LE bytes ready to hand to STREAM_DATA, plus
the sample rate and channel count for STREAM_BEGIN.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass
class WavPcm:
    sample_rate: int
    channels: int
    data: bytes  # raw interleaved PCM16 little-endian (the WAV data chunk)


def read_wav_pcm16(path: Union[str, Path]) -> WavPcm:
    """Read an uncompressed 16-bit PCM WAV. Raises on other formats.

    Non-16 kHz files are accepted but produce a warning -- the device plays at
    16 kHz so the pitch/duration would be off; author clips at 16 kHz.
    """
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(
                f"WAV must be 16-bit PCM (got {w.getsampwidth() * 8}-bit): {path}"
            )
        if w.getcomptype() != "NONE":
            raise ValueError(
                f"WAV must be uncompressed PCM (got {w.getcomptype()}): {path}"
            )
        sample_rate = w.getframerate()
        channels = w.getnchannels()
        data = w.readframes(w.getnframes())

    if sample_rate != 16000:
        import warnings

        warnings.warn(
            f"WAV {path} is {sample_rate} Hz; the device expects 16000 Hz "
            "(pitch/duration will be off). Author clips at 16 kHz.",
            stacklevel=2,
        )
    return WavPcm(sample_rate=sample_rate, channels=channels, data=data)
