"""Keep a returned audio take recoverable until its derived media is verified."""

from hashlib import file_digest, sha256
import json

from connection_files import replace_private


WAV = "return.wav"
RECEIPT = "receipt.json"
PENDING = ".confirmed-return.json"


def retain_provider_audio(profile):
    return profile.get("retain_provider_audio", "no").strip().casefold() in {"yes", "true", "1"}


def record_return(directory, audio, receipt):
    result = {**receipt, "state": "returned", "wav_sha256": sha256(audio).hexdigest()}
    pending = directory / PENDING
    replace_private(pending, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    replace_private(directory / WAV, audio)
    pending.replace(directory / RECEIPT)


def recover_return(directory, receipt):
    """Complete an interrupted commit only for a verified WAV belonging to this submission."""
    pending = directory / PENDING
    if not pending.is_file():
        return receipt
    confirmed = json.loads(pending.read_bytes())
    audio = directory / WAV
    if (receipt.get("state") == "submitting" and confirmed.get("state") == "returned"
            and receipt.get("request_sha256") and all(confirmed.get(key) == receipt.get(key)
                for key in ("request_sha256", "provider_id", "source_id", "reference", "transcript"))
            and audio.is_file() and sha256(audio.read_bytes()).hexdigest() == confirmed.get("wav_sha256")):
        pending.replace(directory / RECEIPT)
        return confirmed
    # An interrupted WAV write or a superseded attempt cannot authorize a new submission.
    pending.unlink()
    return receipt


def load_return(directory, receipt):
    path = directory / WAV
    if receipt["state"] != "returned" or not path.is_file():
        return None
    audio = path.read_bytes()
    if sha256(audio).hexdigest() != receipt.get("wav_sha256"):
        raise ValueError("The pending audio differs from its recorded return.")
    return audio


def finish_return(directory, receipt):
    path = directory / WAV
    if receipt["state"] == "returned" and receipt.get("retained") is False and path.is_file():
        with path.open("rb") as stream:
            checksum = file_digest(stream, "sha256").hexdigest()
        if checksum == receipt.get("wav_sha256"):
            path.unlink(missing_ok=True)
