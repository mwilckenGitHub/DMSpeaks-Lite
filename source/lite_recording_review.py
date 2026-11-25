"""Audition one installed name take and compare its identity before replacement."""
from hashlib import sha256
from lite_legacy_clip_bounds import review_cuts
from lite_store_playback import HASH, SLOT, STATUS, INDEX, playable_saved_names, name_audio_relative, safe_path


def selection(data):
    if not isinstance(data, dict):
        raise ValueError("Choose a saved recording.")
    slot, digest = data.get("slot"), data.get("sha256")
    if not isinstance(slot, str) or not SLOT.fullmatch(slot) or not isinstance(digest, str) or not HASH.fullmatch(digest):
        raise ValueError("Choose a saved recording.")
    return {"slot": slot, "sha256": digest}


def selected_name(player):
    values = player["values"]
    return (values.get("first_name") or values["name"].split(maxsplit=1)[0]).casefold()


def recordings(addons, name, offset):
    if type(offset) is not int or offset < 0:
        raise ValueError("Choose a valid recordings page.")
    entries = sorted((slot, choices[name]) for slot, choices in playable_saved_names(addons).items()
                     if name in choices)
    return {"total": len(entries), "offset": offset,
            "items": [{"slot": slot, "sha256": clip["sha256"], "number": index,
                       **({"legacy_split": True} if clip.get("prefix") else {}),
                       **review_cuts(clip),
                       "caption": clip.get("caption", "")}
                      for index, (slot, clip) in enumerate(entries[offset:offset + 12], offset + 1)]}


def current_take(addons, name, target):
    clip = playable_saved_names(addons).get(target["slot"], {}).get(name)
    if not clip or clip.get("sha256") != target["sha256"]:
        raise ValueError("This recording changed. Open its review again.")
    return clip


def audio(addons, name, target):
    clip = current_take(addons, name, target)
    return _saved_ogg(addons, clip)


def _saved_ogg(addons, clip):
    path = safe_path(addons, name_audio_relative(clip))
    if path.stat().st_size > 32 * 1024 ** 2:
        raise ValueError("The saved name audio is too large to preview.")
    content = path.read_bytes()
    if not content.startswith(b"OggS") or sha256(content).hexdigest() != clip["sha256"]:
        raise ValueError("The saved recording could not be verified.")
    return content


def recording_preview(addons, name, target):
    """Audition exactly the saved phrase, including separately stored older prefixes."""
    clip = current_take(addons, name, target)
    if not clip.get("prefix"):
        return _saved_ogg(addons, clip), "audio/ogg"
    from lite_recording_context import ContextUnavailable, render_audio_parts
    parts = [(name_audio_relative(part), part["sha256"], part["samples"]) for part in (clip["prefix"], clip)]
    try:
        return render_audio_parts(addons, parts), "audio/wav"
    except ContextUnavailable as error:
        raise ValueError("The saved recording could not be verified.") from error
