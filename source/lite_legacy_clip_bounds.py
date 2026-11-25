"""Read measured positions from older name recordings without assuming an original WAV clock."""

import re

from guide_bindings import RATE
from lite_clip_bounds import boundary_method, saved_cuts


def legacy_cut(clip, *, donor_frames=None):
    if "cut" in clip:
        return None
    proof = clip.get("provenance")
    if not isinstance(proof, dict):
        return None
    raw = proof.get("returned_sha256")
    donor = proof.get("paced_return_sha256")
    span = proof.get("donor_samples")
    if (not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in (raw, donor))
            or not isinstance(span, list) or len(span) != 2 or not all(type(n) is int for n in span)
            or not 0 <= span[0] < span[1] or type(clip.get("samples")) is not int
            or span[1] - span[0] != clip["samples"]):
        return None
    start, stop = span
    method = None
    if "prefix" in clip:
        edge = proof.get("boundary")
        if (not isinstance(edge, dict) or edge.get("sample_rate") != RATE
                or edge.get("return_cut_sample") != start or not isinstance(edge.get("method"), str)):
            return None
        quiet = edge.get("return_waveform_quiet_samples")
        if quiet is not None and (not isinstance(quiet, list) or len(quiet) != 2
                                  or not all(type(n) is int for n in quiet)):
            return None
        method = boundary_method(edge, start, trailing=False)
        scope = "name"
    elif proof.get("source") == "returned_wav":
        scope = "phrase"
    else:
        return None
    result = dict(returned_sha256=raw, donor_sha256=donor, sample_rate=RATE, scope=scope,
                  start_sample=start, stop_sample=stop, start_seconds=start / RATE,
                  stop_seconds=stop / RATE, start_method=method, stop_method=None)
    # Historical manifests lack the donor's complete duration. It can be filled
    # only by checking a copy of that exact WAV. Ignore a stale enrichment if
    # the original provenance, slot shape or audio identity has changed.
    stored = clip.get("legacy_cut")
    if donor_frames is None and isinstance(stored, dict) and {k: stored.get(k) for k in result} == result:
        donor_frames = stored.get("donor_frames")
    if type(donor_frames) is int and stop <= donor_frames:
        result["donor_frames"] = donor_frames
    return result


def review_cuts(clip):
    if "cut" in clip or "native_cut" in clip:
        return saved_cuts(clip)
    evidence = legacy_cut(clip)
    return {"legacy_cut": evidence} if evidence else {}
