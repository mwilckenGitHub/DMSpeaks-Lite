import hashlib
import json
import os

def resident_reference_profile(reference, lab):
    if reference is None:
        return None

    # Validate inputs
    if not isinstance(reference, dict):
        raise ValueError("reference must be a dict")

    required_keys = ['id', 'name', 'path', 'transcript']
    for key in required_keys:
        if key not in reference:
            raise ValueError(f"Missing required key: {key}")

    audio_path = reference['path']
    transcript = reference['transcript']

    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("transcript must be non-empty string")

    # Resolve absolute path
    if not isinstance(audio_path, (str, os.PathLike)):
        raise ValueError("Reference audio path is invalid")
    abs_audio_path = os.path.realpath(audio_path)
    if not os.path.isfile(abs_audio_path):
        raise ValueError(f"Audio file does not exist: {abs_audio_path}")

    # Ensure lab/profiles directory exists (do not create registry)
    profiles_dir = os.path.join(lab, 'profiles')
    if not os.path.isdir(profiles_dir):
        raise ValueError(f"Profiles directory does not exist: {profiles_dir}")

    # Hash audio bytes and transcript UTF8
    with open(abs_audio_path, 'rb') as f:
        audio_bytes = f.read()

    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
    sha256 = hashlib.sha256(json.dumps([abs_audio_path,audio_sha256,transcript],ensure_ascii=False).encode('utf-8'))

    # Derive deterministic safe ID
    id_hex = sha256.hexdigest()[:40]
    profile_id = f"studio-{id_hex}"

    # Check for existing profile
    profile_path = os.path.join(profiles_dir, f"{profile_id}.json")

    if os.path.exists(profile_path):
        with open(profile_path, 'r', encoding='utf-8') as f:
            existing = json.load(f)

        # Validate existing profile matches
        expected = {
            'voice_slot': profile_id,
            'reference_file': abs_audio_path,
            'reference_text': transcript,
            'reference_sha256': audio_sha256
        }

        for key, value in expected.items():
            if existing.get(key) != value:
                raise ValueError(f"Existing profile {profile_id} has conflicting data")

        return profile_id

    # Create new profile
    profile_data = {
        'voice_slot': profile_id,
        'reference_file': abs_audio_path,
        'reference_text': transcript,
        'reference_sha256': audio_sha256
    }

    try:
        with open(profile_path, 'x', encoding='utf-8') as f:
            json.dump(profile_data, f, indent=2)
    except FileExistsError:
        return resident_reference_profile(reference, lab)

    return profile_id
