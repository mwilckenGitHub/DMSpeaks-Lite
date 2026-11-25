"""Provider-scoped speaker choices for contextual name generation; local Qwen draft reviewed."""
def batch_speaker_request(record, data, references):
    if not isinstance(data, dict):
        raise ValueError("data must be a dict")
    if "voice_bindings" not in data:
        return data.copy()
    bindings = data["voice_bindings"]
    if not isinstance(bindings, dict):
        raise ValueError("voice_bindings must be a dict")
    refs = [r for r in references if isinstance(r, dict) and isinstance(r.get("id"), str)]
    valid_ids = {r["id"] for r in refs}
    def get_identity(val):
        if isinstance(val, str) and val:
            return val
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(val)
        return None
    def get_key(rec):
        if not isinstance(rec, dict):
            return ""
        meta = (rec.get("metadata") or {})
        speaker = (rec.get("speaker") or {})
        npc_id = get_identity(meta.get("npc_id")) or get_identity(speaker.get("id"))
        fallback = get_identity(rec.get("id"))
        dataset = str(rec.get("dataset", ""))
        if npc_id:
            return f"{dataset}:{npc_id}"
        return f"{dataset}:line:{fallback or ''}"
    key = get_key(record)
    binding = bindings.get(key)
    if isinstance(binding, dict):
        ref_id = binding.get("reference_id")
        voice = binding.get("voice")
        ref_id = ref_id if isinstance(ref_id, str) and ref_id in valid_ids else ""
        voice = voice if isinstance(voice, str) else ""
        result = data.copy()
        result["reference_id"] = ref_id
        result["voice"] = voice
        return result
    meta = (record.get("metadata") or {}) if isinstance(record, dict) else {}
    speaker = (record.get("speaker") or {}) if isinstance(record, dict) else {}
    profile = get_identity(meta.get("npc_voice_profile")) or get_identity(speaker.get("voice_profile_ref"))
    matched = None
    if profile:
        for r in refs:
            if r.get("id") == profile or r.get("fish_profile") == profile or profile in r.get('voice_profile_refs',[]):
                matched = r
                break
    result = data.copy()
    result["reference_id"] = matched.get("id", "") if matched else ""
    result["voice"] = ""
    return result
