"""Ordered Fish voice conditioning, shared by authoring and desktop playback tools."""
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import wave

def read_reference(item):
    if not isinstance(item, dict):
        raise ValueError('Provide Fish references as audio and transcript pairs.')
    transcript = item.get('transcript')
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError('Fish reference audio needs its exact spoken transcript.')
    audio = item.get('wav_bytes')
    if audio is None:
        path = Path(item.get('path') or item.get('uri') or '')
        if not path.is_file() or not 0 < path.stat().st_size <= 20_000_000:
            raise ValueError('Choose an accessible voice reference under 20 MB.')
        if path.suffix.lower() not in {'.wav', '.mp3', '.flac'}:
            raise ValueError('Use a WAV, MP3 or FLAC reference for the hosted Fish service.')
        audio = path.read_bytes()
    elif not isinstance(audio, bytes) or audio[:4] != b'RIFF' or audio[8:12] != b'WAVE':
        raise ValueError('The in-memory Fish reference must be a WAV recording.')
    if not 0 < len(audio) <= 20_000_000:
        raise ValueError('Choose an accessible voice reference under 20 MB.')
    digest = sha256(audio).hexdigest()
    expected = item.get('content_sha256') or item.get('sha256')
    if expected and digest != expected:
        raise ValueError('The voice reference changed. Select the current recording before sending it.')
    return dict(audio=audio, text=transcript), dict(
        id=item.get('id'), audio_sha256=digest,
        transcript_sha256=sha256(transcript.encode('utf-8')).hexdigest())

def reference_budget(reference):
    """Apply the shared project budget before either Fish transport is built."""
    ordered = reference if isinstance(reference, (list, tuple)) else [reference]
    if not ordered:
        raise ValueError("Provide the ordered voice and dialogue references.")
    import soundfile as sf
    duration = Fraction(0)
    for item in ordered:
        sample, _ = read_reference(item)
        try:
            with sf.SoundFile(BytesIO(sample["audio"])) as audio:
                if audio.frames <= 0 or audio.samplerate <= 0:
                    raise ValueError("The voice reference has no bounded sample clock.")
                duration += Fraction(audio.frames, audio.samplerate)
        except sf.LibsndfileError as error:
            raise ValueError("The voice reference has an unreadable sample clock.") from error
        if duration > 120:
            raise ValueError("The ordered Fish references exceed the shared 120-second budget.")
    return duration


def joined_fish_reference(reference):
    """Join the voice reference and complete dialogue on one sample clock."""
    ordered = reference if isinstance(reference, (list, tuple)) else [reference]
    if not ordered:
        raise ValueError("Provide the ordered voice and dialogue references.")
    samples, evidence = zip(*(read_reference(ref) for ref in ordered), strict=True)
    reference_bytes = sum(len(sample["audio"]) for sample in samples)
    frames = []
    count = 0
    for sample in samples:
        with wave.open(BytesIO(sample["audio"]), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 44100):
                raise ValueError("The ordered Fish reference must use 44.1 kHz mono PCM WAV.")
            count += audio.getnframes()
            if count > 120 * 44100:
                raise ValueError("The ordered Fish reference exceeds the established 120-second cap.")
            payload = audio.readframes(audio.getnframes())
            if len(payload) != audio.getnframes() * 2:
                raise ValueError("The ordered Fish reference is incomplete.")
            frames.append(payload)
    with BytesIO() as out:
        with wave.open(out, "wb") as wav:
            wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            wav.writeframes(b"".join(frames))
        joined = out.getvalue()
    result = dict(id=ordered[0]["id"], wav_bytes=joined, sha256=sha256(joined).hexdigest(),
                  transcript="\n\n".join(item["text"] for item in samples))
    return result, list(evidence) if isinstance(reference, (list, tuple)) else evidence[0], reference_bytes
