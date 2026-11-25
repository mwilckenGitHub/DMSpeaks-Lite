import json
import hashlib
import math
from fractions import Fraction
from pathlib import Path
from typing import List, Any, Dict, Optional
import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parent / "formats" / "open-speech-corpus-0.1.schema.json"

def _load_schema():
    with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def _media_duration_us(media):
    """Nearest microsecond of the declared clock; decoded frame count wins.

    Integer word times cannot represent every sample boundary exactly. Rounding
    the bound, rather than allowing one sample of word overrun, admits only the
    representation precision. Redundant seconds must agree within one frame.
    """
    seconds = media.get('seconds')
    try:
        seconds_clock = Fraction(str(seconds)) if seconds is not None else None
    except (ValueError, OverflowError):
        raise ValueError('seconds must be finite') from None
    frames, rate = media.get('frames'), media.get('sample_rate')
    if frames is not None and rate is not None:
        duration = Fraction(frames, rate)
        if seconds_clock is not None and abs(seconds_clock - duration) > Fraction(1, rate):
            raise ValueError('seconds disagrees with frames/sample_rate by more than one frame')
    else:
        duration = seconds_clock
    return round(duration * 1_000_000) if duration is not None else None

def validate_corpus(root: Any, supported_extensions: tuple = ()) -> List[str]:
    errors = []
    if not isinstance(root, dict):
        return ["Root must be a JSON object"]

    try:
        schema = _load_schema()
        validator = jsonschema.Draft202012Validator(schema)
        for error in validator.iter_errors(root):
            path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else "root"
            errors.append(f"Schema error at {path}: {error.message}")
        if errors:
            return errors
    except Exception as e:
        return [f"Failed to validate schema: {str(e)}"]

    # Build ID sets
    def get_ids(key):
        items = root.get(key, [])
        ids = set()
        for item in items:
            if isinstance(item, dict) and 'id' in item:
                ids.add(item['id'])
        return ids

    entry_ids = get_ids("entries")
    speaker_ids = get_ids("speakers")
    voice_profile_ids = get_ids("voice_profiles")
    media_ids = get_ids("media")
    audio_source_ids = get_ids("audio_sources")
    take_ids = get_ids("takes")
    clip_ids = get_ids("clips")

    # Check unique IDs
    def check_unique(key, ids_set):
        items = root.get(key, [])
        seen = set()
        for item in items:
            if isinstance(item, dict) and 'id' in item:
                iid = item['id']
                if iid in seen:
                    errors.append(f"Duplicate ID '{iid}' in {key}")
                seen.add(iid)

    check_unique("entries", entry_ids)
    check_unique("speakers", speaker_ids)
    check_unique("voice_profiles", voice_profile_ids)
    check_unique("media", media_ids)
    check_unique("audio_sources", audio_source_ids)
    check_unique("takes", take_ids)
    check_unique("clips", clip_ids)

    media_duration_us = {}
    for media in root.get('media', []):
        try:
            media_duration_us[media['id']] = _media_duration_us(media)
        except ValueError as error:
            errors.append(f"Media '{media['id']}': {error}")

    def matches_type(value,kind):
        if kind=='string':return isinstance(value,str)
        if kind=='boolean':return isinstance(value,bool)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return False
        return kind=='number' or kind=='integer' and (isinstance(value,int) or value.is_integer())
    for entry in root.get('entries',[]):
        for name,definition in entry.get('variables',{}).items():
            kind=definition.get('type')
            if 'default' in definition and not matches_type(definition['default'],kind):errors.append(f"Entry '{entry['id']}', variable '{name}': default does not match {kind}")
            if any(not matches_type(value,kind) for value in definition.get('enum',[])):errors.append(f"Entry '{entry['id']}', variable '{name}': enum values do not match {kind}")
            if 'default' in definition and 'enum' in definition and definition['default'] not in definition['enum']:errors.append(f"Entry '{entry['id']}', variable '{name}': default is not an allowed enum value")
    # Check references
    for entry in root.get("entries", []):
        if not isinstance(entry, dict): continue
        eid = entry.get("id", "?")
        if "speaker_ref" in entry and entry["speaker_ref"] not in speaker_ids:
            errors.append(f"Entry '{eid}': speaker_ref '{entry['speaker_ref']}' not found in speakers")

        # Check required extensions
        ext = entry.get("extensions", {})
        if isinstance(ext, dict) and "required" in ext:
            req_exts = ext["required"]
            if isinstance(req_exts, list):
                for ext_name in req_exts:
                    if ext_name not in supported_extensions:
                        errors.append(f"Entry '{eid}': required extension '{ext_name}' not supported")

    # Check root required extensions
    root_req_exts = root.get("required_extensions", [])
    if isinstance(root_req_exts, list):
        for ext_name in root_req_exts:
            if ext_name not in supported_extensions:
                errors.append(f"Root: required extension '{ext_name}' not supported")

    # Registry references are checked even when attached as extension metadata.
    for registry in ('entries','speakers','voice_profiles','takes','clips'):
        for item in root.get(registry,[]):
            for field,known in [('speaker_ref',speaker_ids),('entry_ref',entry_ids),('media_ref',media_ids),('voice_profile_ref',voice_profile_ids)]:
                if field in item and item[field] not in known:
                    errors.append(f"{registry} '{item['id']}': {field} not found in registry")

    for item in root.get('audio_sources',[]):
        for profile in item.get('voice_profile_refs',[]):
            if profile not in voice_profile_ids:errors.append(f"Audio source '{item['id']}': voice profile '{profile}' not found")
    # Check takes
    for take in root.get("takes", []):
        if not isinstance(take, dict): continue
        tid = take.get("id", "?")

        # Check entry_ref
        if "entry_ref" in take and take["entry_ref"] not in entry_ids:
            errors.append(f"Take '{tid}': entry_ref '{take['entry_ref']}' not found in entries")

        # Check media_ref
        if "media_ref" in take and take["media_ref"] not in media_ids:
            errors.append(f"Take '{tid}': media_ref '{take['media_ref']}' not found in media")

        # Check source_revision if words present
        words = take.get("words", [])
        if words:
            entry_ref = take.get("entry_ref")
            if entry_ref and entry_ref in entry_ids:
                entry = next((e for e in root.get("entries", []) if isinstance(e, dict) and e.get("id") == entry_ref), None)
                if entry:
                    source_text = entry.get("source", {}).get("text", "")
                    expected_rev = _sha256(source_text)
                    actual_rev = take.get("source_revision", "")
                    history=entry.get('source_history',{})
                    if actual_rev != expected_rev and (actual_rev not in history or _sha256(history[actual_rev])!=actual_rev):
                        errors.append(f"Take '{tid}': source_revision does not match SHA256 of entry source text")

        # Check word timings
        if words:
            prev_start = -1
            prev_end = -1
            for i, word in enumerate(words):
                if not isinstance(word, dict): continue
                start_us = word.get("start_us", 0)
                end_us = word.get("end_us", 0)
                source_start = word.get("source_start", 0)
                source_end = word.get("source_end", 0)

                if end_us < start_us:
                    errors.append(f"Take '{tid}', word {i}: end_us < start_us")
                if source_end < source_start:
                    errors.append(f"Take '{tid}', word {i}: source_end < source_start")
                if start_us < prev_start or end_us < prev_end:
                    errors.append(f"Take '{tid}', word {i}: start_us not non-decreasing")
                prev_start = start_us
                prev_end = end_us

                # Check source bounds against entry text
                entry_ref = take.get("entry_ref")
                if entry_ref and entry_ref in entry_ids:
                    entry = next((e for e in root.get("entries", []) if isinstance(e, dict) and e.get("id") == entry_ref), None)
                    if entry:
                        source_text = entry.get("source", {}).get("text", "")
                        source_text = entry.get('source_history',{}).get(take.get('source_revision'),source_text)
                        text_len = len(source_text)
                        if source_start > text_len:
                            errors.append(f"Take '{tid}', word {i}: source_start exceeds text length")
                        if source_end > text_len:
                            errors.append(f"Take '{tid}', word {i}: source_end exceeds text length")

        # Check last end against media duration
        if words:
            last_end_us = max(w.get("end_us", 0) for w in words if isinstance(w, dict))
            media_ref = take.get("media_ref")
            if media_ref and media_ref in media_ids:
                media = next((m for m in root.get("media", []) if isinstance(m, dict) and m.get("id") == media_ref), None)
                if media:
                    max_us = media_duration_us.get(media_ref)

                    if max_us is not None and last_end_us > max_us:
                        errors.append(f"Take '{tid}': last word end_us exceeds media duration")

    # Check clips
    for clip in root.get("clips", []):
        if not isinstance(clip, dict): continue
        cid = clip.get("id", "?")

        # Check entry_ref
        if "entry_ref" in clip and clip["entry_ref"] not in entry_ids:
            errors.append(f"Clip '{cid}': entry_ref '{clip['entry_ref']}' not found in entries")

        # Check slots
        slots = clip.get("slots", [])
        prev_slot_order = -1
        seen_slot_orders = set()

        # Get entry variables if entry_ref exists
        entry_vars = {}
        entry_ref = clip.get("entry_ref")
        if entry_ref and entry_ref in entry_ids:
            entry = next((e for e in root.get("entries", []) if isinstance(e, dict) and e.get("id") == entry_ref), None)
            if entry:
                entry_vars = entry.get("variables", {})

        for i, slot in enumerate(slots):
            if not isinstance(slot, dict): continue
            slot_order = slot.get("slot_order", -1)

            if slot_order <= 0:
                errors.append(f"Clip '{cid}', slot {i}: slot_order must be positive")
            if slot_order <= prev_slot_order:
                errors.append(f"Clip '{cid}', slot {i}: slot_order not strictly increasing")
            if slot_order in seen_slot_orders:
                errors.append(f"Clip '{cid}', slot {i}: duplicate slot_order")
            seen_slot_orders.add(slot_order)
            prev_slot_order = slot_order

            kind = slot.get("kind")
            if kind == "variable":
                var_ref = slot.get("variable_ref")
                if var_ref is None:
                    errors.append(f"Clip '{cid}', slot {i}: variable kind requires variable_ref")
                elif var_ref not in entry_vars:
                    errors.append(f"Clip '{cid}', slot {i}: variable_ref '{var_ref}' not found in entry variables")

            # Check media_ref if present
            if "media_ref" in slot and slot["media_ref"] not in media_ids:
                errors.append(f"Clip '{cid}', slot {i}: media_ref '{slot['media_ref']}' not found in media")

    return errors
