"""Exact half-open intervals on the WAV clock used to produce a name clip."""

import re

from guide_bindings import RATE


def boundary_method(edge, frame, *, trailing):
    quiet = edge.get("return_waveform_quiet_samples")
    if quiet and quiet[0] <= frame < quiet[1] and not edge.get("return_closure_phone"):
        return "waveform_decay" if trailing else "waveform_quiet"
    return edge["method"]


def clip_bounds(returned_sha256, donor_sha256, donor_frames, start, stop, start_method, stop_method):
    for checksum in (returned_sha256, donor_sha256):
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("The name cut needs the identity of its returned WAV and its decoded donor.")
    if (any(type(frame) is not int for frame in (donor_frames, start, stop))
            or not 0 <= start < stop <= donor_frames):
        raise ValueError("The name cut lies outside the donor recording.")
    if not all(isinstance(method, str) and method.strip() for method in (start_method, stop_method)):
        raise ValueError("The name cut needs its measured boundary methods.")
    return dict(returned_sha256=returned_sha256, donor_sha256=donor_sha256, donor_frames=donor_frames,
                start_sample=start, stop_sample=stop, sample_rate=RATE,
                start_seconds=start / RATE, stop_seconds=stop / RATE,
                start_method=start_method, stop_method=stop_method)


def checked_cut(cut, clip_samples=None):
    if not isinstance(cut, dict):
        raise ValueError("The saved name cut is missing its donor interval.")
    expected = clip_bounds(*(cut.get(key) for key in ("returned_sha256", "donor_sha256", "donor_frames",
                                                       "start_sample", "stop_sample", "start_method", "stop_method")))
    if cut != expected or (clip_samples is not None and
                           (type(clip_samples) is not int or cut["stop_sample"] - cut["start_sample"] != clip_samples)):
        raise ValueError("The saved name cut disagrees with its decoded recording clock.")
    return expected


def saved_cuts(clip):
    """Old clips can lack timing evidence. Never derive donor bounds from an Ogg duration."""
    if "cut" not in clip:
        if "native_cut" in clip:
            raise ValueError("The paced name clip is missing its encoded donor interval.")
        return {}
    cut = checked_cut(clip["cut"], clip["samples"])
    phrase = clip.get("context_phrase") or clip.get("provenance") or {}
    if phrase.get("source") == "returned_wav" and (
            phrase.get("returned_sha256") != cut["returned_sha256"]
            or phrase.get("paced_return_sha256") != cut["donor_sha256"]
            or phrase.get("donor_samples") != [cut["start_sample"], cut["stop_sample"]]):
        raise ValueError("The saved cut and its returned phrase describe different audio.")
    result = {"cut": cut}
    if "native_cut" in clip:
        native = checked_cut(clip["native_cut"])
        if not (native["donor_sha256"] == native["returned_sha256"] == cut["returned_sha256"]):
            raise ValueError("The native and paced cuts refer to different returns.")
        result["native_cut"] = native
    return result
