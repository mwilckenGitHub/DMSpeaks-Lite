from typing import List, Dict, Any

def has_partial_timing(asset):
    source = asset.get('timing_source')
    return asset.get('timing_partial') is True or isinstance(source, str) and source.startswith('partial_')

def summarize_timing(assets: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not assets:
        return {"timing_source": "unavailable", "timing_sources": [], "timing_partial": False}

    sources = set()
    partial = False
    has_words = False

    for asset in assets:
        words = asset.get("words")
        if words:
            has_words = True
            src = asset.get("timing_source") or "unavailable"
            if not isinstance(src, str): src = "unavailable"
            sources.add(src)
            partial |= has_partial_timing(asset)
        else:
            partial = True

    if not has_words:
        return {"timing_source": "unavailable", "timing_sources": [], "timing_partial": False}

    sorted_sources = sorted(sources)

    if any("estimated" in s.lower() for s in sorted_sources):
        base = "estimated_assembled_clip_timings"
    elif all(s == "provider" for s in sorted_sources):
        base = "provider_assembled_clip_timings"
    else:
        base = "assembled_from_selected_clip_timings"

    if partial:
        base = f"partial_{base}"

    return {
        "timing_source": base,
        "timing_sources": sorted_sources,
        "timing_partial": partial
    }
