"""Measurements of original clip edges used to verify newly generated Name joins."""

from base64 import b64decode, b64encode
from binascii import Error
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
import json

import numpy as np
import soundfile as sf

from guide_bindings import RATE
from lite_portable_io import clip_binding, digest


MARGIN = round(RATE * .020)


def measure_edge(samples, side):
    if side == "head":
        endpoint = samples[0]
        window = samples[:MARGIN]
    else:
        endpoint = samples[-1]
        window = samples[-MARGIN:]
    steps = np.sort(np.abs(np.diff(window))) if len(window) == MARGIN else []
    return np.concatenate((np.asarray([endpoint], dtype="<f4"), np.asarray(steps, dtype="<f4")))


def discontinuous(left, right):
    if len(left) != MARGIN or len(right) != MARGIN:
        return False
    jump = float(np.abs(left[0] - right[0]))
    if jump <= .01:
        return False
    nearby = float(np.quantile(np.concatenate((left[1:], right[1:])), .99))
    return jump > 3 * nearby


def choices(part):
    return (part["clip"],) if part["kind"] == "fixed" else part["choices"].values()


def required_edges(sequence):
    edges = defaultdict(set)
    for left, right in zip(sequence, sequence[1:]):
        if right["kind"] == "name":
            for clip in choices(left):
                edges[clip["file"]].add("tail")
        if left["kind"] == "name":
            for clip in choices(right):
                edges[clip["file"]].add("head")
    return edges


def sequence_bindings(sequence):
    result = {}
    for part in sequence:
        for clip in choices(part):
            binding = clip_binding(clip)
            if result.setdefault(clip["file"], binding) != binding:
                raise ValueError("Two seed clips disagree on their audio identity.")
    return result


def pack_seed_joins(row, bundled):
    from lite_portable_companion import verified_companion

    manifest = bundled["dialogue.json"]
    if len(manifest) > 1_000_000 or digest(manifest) != row["companion_seed"]["manifest_sha256"]:
        raise ValueError("The original playback seed differs from its dialogue binding.")
    sequence, files = verified_companion(row, manifest, None, clip_bytes=bundled)
    if set(bundled) != files | {"dialogue.json"}:
        raise ValueError("The original playback seed contains unrelated files.")
    edges = {}
    for filename, sides in required_edges(sequence).items():
        pcm, _ = sf.read(BytesIO(bundled[filename]), dtype="float32")
        edges[filename] = {side: b64encode(measure_edge(pcm, side).tobytes()).decode("ascii")
                           for side in sides}
    return json.dumps(edges, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class SeedJoins:
    clips: dict
    edges: dict

    def edge(self, folder, clip, side):
        name = clip["file"]
        if self.clips.get(name) == clip_binding(clip):
            evidence = self.edges.get(name, {}).get(side)
            if evidence is None:
                raise ValueError("The playback seed is missing a measured Name join.")
            return evidence
        from lite_portable_render import wav_pcm

        return measure_edge(wav_pcm(folder / name), side)


def read_seed_joins(raw, checksum, sequence):
    if digest(raw) != checksum:
        raise ValueError("The measured Name joins differ from the released playback seed.")
    expected = required_edges(sequence)
    clips = sequence_bindings(sequence)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.keys() != expected.keys():
            raise ValueError("The playback seed does not measure every adjacent Name edge.")
        edges = {}
        for filename, sides in data.items():
            if not isinstance(sides, dict) or sides.keys() != expected[filename]:
                raise ValueError("The playback seed has the wrong adjacent Name edges.")
            samples = clips[filename][1]
            length = MARGIN if samples >= MARGIN else 1
            measurements = {}
            for side, encoded in sides.items():
                values = np.frombuffer(b64decode(encoded, validate=True), dtype="<f4")
                if (len(values) != length or not np.isfinite(values).all()
                        or np.any(values[1:] < 0) or np.any(np.diff(values[1:]) < 0)):
                    raise ValueError("The measured Name join has a malformed sample window.")
                measurements[side] = values
            edges[filename] = measurements
    except (TypeError, Error, UnicodeError) as error:
        raise ValueError("The measured Name joins are invalid.") from error
    return SeedJoins(clips, edges)
