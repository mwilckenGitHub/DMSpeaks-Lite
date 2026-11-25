"""Read the measured source PCM needed to separate a name from its fixed prefix."""
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np

from lite_portable_join import JoinWaveform

MANIFEST_SHA256 = "899953333aed9161083437137058f744fe736d4243a5666d6a5ee70a9fe4808c"
ARCHIVE = Path(__file__).resolve().parents[1] / "media" / f"paced-prefix-{MANIFEST_SHA256}.zip"
MAX_FRAMES = 120 * 44100


@lru_cache(maxsize=1)
def _sources():
    with ZipFile(ARCHIVE) as archive:
        info = archive.getinfo("manifest.json")
        if info.file_size > 8 * 1024 * 1024:
            raise ValueError("The measured prefix catalog is too large.")
        payload = archive.read(info)
    if sha256(payload).hexdigest() != MANIFEST_SHA256:
        raise ValueError("The measured prefix catalog differs from the installed version.")
    manifest = json.loads(payload)
    if manifest.get("format") != "dmspeaks-paced-prefix-pcm-1":
        raise ValueError("The measured prefix catalog has an unsupported format.")
    return manifest["sources"]


def prefix_waveform(wave_sha256, frames):
    """The pinned catalog binds each PCM range to the exact measured WAV and clock."""
    if not ARCHIVE.is_file():
        return None
    record = _sources().get(wave_sha256)
    if record is None:
        return None
    if record["frames"] != frames or not 0 < frames <= MAX_FRAMES:
        raise ValueError("The measured prefix has a different source clock.")
    spans = record["spans"]
    previous_end = -1
    size = 0
    for start, end in spans:
        if not 0 <= start < end <= frames or start < previous_end:
            raise ValueError("The measured prefix has invalid sample ranges.")
        previous_end = end
        size += 4 * (end - start)
    with ZipFile(ARCHIVE) as archive:
        info = archive.getinfo(wave_sha256 + ".f32")
        if info.file_size != size:
            raise ValueError("The measured prefix has an incorrect sample count.")
        data = archive.read(info)
    if sha256(data).hexdigest() != record["sha256"]:
        raise ValueError("The measured prefix PCM differs from the catalog.")
    samples = np.frombuffer(data, dtype="<f4")
    if not np.isfinite(samples).all():
        raise ValueError("The measured prefix has invalid samples.")
    segments = []
    offset = 0
    for start, end in spans:
        length = end - start
        segments.append((start, samples[offset:offset + length]))
        offset += length
    return JoinWaveform(segments, frames)
