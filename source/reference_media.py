"""Portable lossless reference assets with exact original WAV reconstruction."""

from base64 import b64decode, b64encode
from hashlib import sha256
from io import BytesIO
import re
import struct
import wave

import numpy as np
import soundfile as sf


FORMAT = "dmspeaks-reference-media-1"
RATE = 44100
MAX_SAMPLES = 120 * RATE
MAX_BYTES = 20_000_000
HASH = re.compile(r"[0-9a-f]{64}")
FIELDS = {"format", "wav_sha256", "flac_sha256", "pcm_sha256", "sample_rate", "samples",
          "wave_prefix", "wave_suffix"}
ENVELOPE_FIELDS = FIELDS - {"format", "flac_sha256"}


def digest(data):
    return sha256(data).hexdigest()


def wave_data(raw):
    if (not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_BYTES
            or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE"
            or struct.unpack_from("<I", raw, 4)[0] != len(raw) - 8):
        raise ValueError("The reference needs a complete bounded PCM WAV recording.")
    cursor = 12
    found = None
    while cursor + 8 <= len(raw):
        tag = raw[cursor:cursor + 4]
        length = struct.unpack_from("<I", raw, cursor + 4)[0]
        start, end = cursor + 8, cursor + 8 + length
        if end > len(raw):
            raise ValueError("The reference WAV contains an incomplete chunk.")
        if tag == b"data":
            if found is not None:
                raise ValueError("The reference WAV has competing sample payloads.")
            found = (start, end)
        cursor = end + (length & 1)
    if cursor != len(raw) or found is None:
        raise ValueError("The reference WAV has an invalid sample layout.")
    with wave.open(BytesIO(raw), "rb") as source:
        if ((source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype())
                != (1, 2, RATE, "NONE") or not 0 < source.getnframes() <= MAX_SAMPLES):
            raise ValueError("The reference needs mono 44.1 kHz PCM16 within the reference budget.")
        frames = source.getnframes()
        data = source.readframes(frames)
    lo, hi = found
    if data != raw[lo:hi] or len(data) != 2 * frames:
        raise ValueError("The reference sample clock differs from its WAV payload.")
    return lo, hi, data


def wave_envelope(raw):
    lo, hi, pcm = wave_data(raw)
    prefix, suffix = raw[:lo], raw[hi:]
    if len(prefix) + len(suffix) > 65536:
        raise ValueError("The reference WAV has excessive non-audio metadata.")
    return {"wav_sha256": digest(raw), "pcm_sha256": digest(pcm), "sample_rate": RATE,
            "samples": len(pcm) // 2, "wave_prefix": b64encode(prefix).decode("ascii"),
            "wave_suffix": b64encode(suffix).decode("ascii")}


def restore_wave(envelope, pcm):
    if (not isinstance(envelope, dict) or envelope.keys() != ENVELOPE_FIELDS
            or envelope["sample_rate"] != RATE or type(envelope["samples"]) is not int
            or not 0 < envelope["samples"] <= MAX_SAMPLES or not isinstance(pcm, bytes)
            or len(pcm) != envelope["samples"] * 2
            or any(not isinstance(envelope[key], str) for key in ("wave_prefix", "wave_suffix"))
            or len(envelope["wave_prefix"]) + len(envelope["wave_suffix"]) > 90000
            or digest(pcm) != envelope["pcm_sha256"]):
        raise ValueError("The assembled reference differs from its checked PCM envelope.")
    try:
        prefix = b64decode(envelope["wave_prefix"], validate=True)
        suffix = b64decode(envelope["wave_suffix"], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("The assembled reference WAV metadata is unreadable.") from error
    if len(prefix) + len(suffix) > 65536:
        raise ValueError("The assembled reference has excessive non-audio metadata.")
    raw = prefix + pcm + suffix
    if digest(raw) != envelope["wav_sha256"]:
        raise ValueError("The assembled reference differs from the exact checked WAV.")
    wave_data(raw)
    return raw


def pack_wave(raw):
    envelope = wave_envelope(raw)
    _, _, pcm = wave_data(raw)
    samples = np.frombuffer(pcm, dtype="<i2")
    compressed = BytesIO()
    sf.write(compressed, samples, RATE, format="FLAC", subtype="PCM_16")
    flac = compressed.getvalue()
    return {"format": FORMAT, "flac_sha256": digest(flac), **envelope}, flac


def unpack_wave(metadata, flac):
    if (not isinstance(metadata, dict) or metadata.keys() != FIELDS or metadata["format"] != FORMAT
            or metadata["sample_rate"] != RATE or type(metadata["samples"]) is not int
            or not 0 < metadata["samples"] <= MAX_SAMPLES
            or any(not isinstance(metadata[key], str) or not HASH.fullmatch(metadata[key])
                   for key in ("wav_sha256", "flac_sha256", "pcm_sha256"))
            or any(not isinstance(metadata[key], str) for key in ("wave_prefix", "wave_suffix"))
            or len(metadata["wave_prefix"]) + len(metadata["wave_suffix"]) > 90000
            or not isinstance(flac, bytes) or not 0 < len(flac) <= MAX_BYTES
            or digest(flac) != metadata["flac_sha256"]):
        raise ValueError("The portable reference differs from its checked media contract.")
    try:
        prefix = b64decode(metadata["wave_prefix"], validate=True)
        suffix = b64decode(metadata["wave_suffix"], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("The reference WAV metadata is unreadable.") from error
    if len(prefix) + len(suffix) > 65536:
        raise ValueError("The reference WAV has excessive non-audio metadata.")
    try:
        with sf.SoundFile(BytesIO(flac)) as source:
            if (source.format, source.subtype, source.samplerate, source.channels, source.frames) != (
                    "FLAC", "PCM_16", RATE, 1, metadata["samples"]):
                raise ValueError("The portable reference has a different decoded sample clock.")
            pcm = source.read(dtype="int16").astype("<i2", copy=False).tobytes()
    except sf.LibsndfileError as error:
        raise ValueError("The portable reference audio is unreadable.") from error
    if digest(pcm) != metadata["pcm_sha256"]:
        raise ValueError("The portable reference samples differ from the checked recording.")
    raw = prefix + pcm + suffix
    if digest(raw) != metadata["wav_sha256"]:
        raise ValueError("The reconstructed reference differs from the exact original WAV.")
    wave_data(raw)
    return raw
