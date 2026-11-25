import json
import math
import re
import base64
import hashlib
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape, quoteattr
import xml.etree.ElementTree as ET

def _validate_rate(rate):
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0.5 or rate > 2.0:
        raise ValueError("Rate must be between 0.5 and 2.0")

def _clean_text(text, capabilities, syntax):
    if not isinstance(text,str):raise ValueError('Speech input must be text')
    if syntax == 'brackets':return text
    return re.sub(r'\[[^\]\n]*\]', '', text).strip()

def _get_directions(text, capabilities):
    caps = set(c.strip() for c in capabilities.split(',') if c.strip())
    if 'instructions' not in caps:
        return ""
    matches = re.findall(r'\[(.*?)\]', text)
    return " ".join(matches) if matches else ""

def _build_fish(profile, text, instructions, rate, voice, secret, reference=None):
    endpoint = profile.get('endpoint', '').rstrip('/')
    model = profile.get('model', 'drama-3')
    if (urlsplit(endpoint).hostname == 'api.fish.audio' and
            model not in {'s1', 's2-pro', 's2.1-pro', 's2.1-pro-free', 'drama-3'}):
        raise ValueError('Choose an exact supported Fish model ID; an unknown model could select a paid default.')
    caps = profile.get('capabilities', '')
    if not voice and not reference:
        raise ValueError("Voice is required for Fish TTS")
    clean_text = _clean_text(text, caps, profile.get('syntax', ''))
    if instructions and not clean_text.lstrip().startswith("[" + instructions + "]"):
        clean_text = "[" + instructions + "] " + clean_text
    body = {
        "text": clean_text,
        "reference_id": voice,
        "format": "wav",
        "sample_rate": 44100,
        "normalize": True
    }
    if 'speed' in caps:
        body["prosody"] = {"speed": rate}
    content_type = 'application/json'
    reference_evidence = None
    if reference:
        ordered = reference if isinstance(reference, (tuple, list)) else (reference,)
        samples, evidence = [], []
        for item in ordered:
            sample, binding = _fish_reference(item)
            samples.append(sample)
            evidence.append(binding)
        reference_evidence = evidence[0] if isinstance(reference, dict) else evidence
        try:
            import msgpack
        except ImportError:
            raise ValueError('Install the application requirements to send binary Fish voice references.') from None
        body.pop('reference_id', None)
        body['references'] = samples
        body['latency'] = 'normal'
        encoded = msgpack.packb(body, use_bin_type=True)
        content_type = 'application/msgpack'
    else:
        encoded = json.dumps(body).encode('utf-8')
    return {
        "method": "POST",
        "url": endpoint + ("/v1/tts/stream/with-timestamp" if "timestamps" in caps else "/v1/tts"),
        "headers": {
            "Authorization": f"Bearer {secret}",
            "Content-Type": content_type,
            "Accept": "text/event-stream" if "timestamps" in caps else "audio/wav",
            "model": model
        },
        "body": encoded,
        "reference_evidence": reference_evidence
    }

def _fish_reference(item):
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
    digest = hashlib.sha256(audio).hexdigest()
    expected = item.get('content_sha256') or item.get('sha256')
    if expected and digest != expected:
        raise ValueError('The voice reference changed. Select the current recording before sending it.')
    return dict(audio=audio, text=transcript), dict(
        id=item.get('id'), audio_sha256=digest,
        transcript_sha256=hashlib.sha256(transcript.encode('utf-8')).hexdigest())

def _build_elevenlabs(profile, text, instructions, rate, voice, secret):
    endpoint = profile.get('endpoint', '').rstrip('/')
    model = profile.get('model', 'eleven_v3')
    caps = profile.get('capabilities', '')
    if not voice:
        raise ValueError("Voice is required for ElevenLabs TTS")
    clean_text = _clean_text(text, caps, profile.get('syntax', ''))
    body = {
        "text": clean_text,
        "model_id": model,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0,
            "use_speaker_boost": True
        }
    }
    if 'speed' in caps and model != "eleven_v3":
        body["voice_settings"]["speed"] = rate
    if 'seed' in profile:
        try:
            body["seed"] = int(profile['seed'])
        except (ValueError, TypeError):
            pass
    return {
        "method": "POST",
        "url": endpoint + "/v1/text-to-speech/" + quote(voice, safe='') + "/with-timestamps",
        "headers": {
            "xi-api-key": secret,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        "body": json.dumps(body).encode('utf-8')
    }

def _build_openai(profile, text, instructions, rate, voice, secret):
    endpoint = profile.get('endpoint', '').rstrip('/')
    if endpoint.endswith('/v1'):
        url = endpoint + "/audio/speech"
    else:
        url = endpoint + "/v1/audio/speech"
    model = profile.get('model', '')
    caps = profile.get('capabilities', '')
    if not voice:
        raise ValueError("Voice is required for OpenAI TTS")
    clean_text = _clean_text(text, caps, profile.get('syntax', ''))
    body = {
        "model": model,
        "input": clean_text,
        "voice": voice,
        "response_format": "wav"
    }
    if 'speed' in caps:
        body["speed"] = rate
    if 'directions' in caps and 'instructions' in profile.get('syntax', '') and instructions:
        body["instructions"] = instructions
    headers = {
        "Content-Type": "application/json",
        "Accept": "audio/wav"
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return {
        "method": "POST",
        "url": url,
        "headers": headers,
        "body": json.dumps(body).encode('utf-8')
    }

def _build_azure(profile, text, instructions, rate, voice, secret):
    endpoint = profile.get('endpoint', '').rstrip('/')
    lang = profile.get('language', 'en-US')
    caps = profile.get('capabilities', '')
    if not voice:
        raise ValueError("Voice is required for Azure TTS")
    clean_text = _clean_text(text, caps, profile.get('syntax', ''))
    if clean_text.strip().startswith('<speak'):
        try:
            ET.fromstring(clean_text)
            body = clean_text
        except ET.ParseError:
            raise ValueError("Invalid SSML for Azure TTS")
    else:
        escaped = escape(clean_text)
        if 'speed' in caps:
            body = f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang={quoteattr(lang)}><voice name={quoteattr(voice)}><prosody rate='{int((rate-1)*100)}%'>{escaped}</prosody></voice></speak>"
        else:
            body = f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang={quoteattr(lang)}><voice name={quoteattr(voice)}>{escaped}</voice></speak>"
    return {
        "method": "POST",
        "url": endpoint + "/cognitiveservices/v1",
        "headers": {
            "Ocp-Apim-Subscription-Key": secret,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
            "Accept": "audio/wav"
        },
        "body": body.encode('utf-8')
    }

def _build_google(profile, text, instructions, rate, voice, secret):
    endpoint = profile.get('endpoint', '').rstrip('/')
    lang = profile.get('language', 'en-US')
    caps = profile.get('capabilities', '')
    if not voice:
        raise ValueError("Voice is required for Google TTS")
    clean_text = _clean_text(text, caps, profile.get('syntax', ''))
    if clean_text.strip().startswith('<speak'):
        try: ET.fromstring(clean_text)
        except ET.ParseError: raise ValueError("Invalid SSML for Google TTS")
        input_data = {"ssml": clean_text}
    else:
        input_data = {"text": clean_text}
    body = {
        "input": input_data,
        "voice": {"name": voice, "languageCode": lang},
        "audioConfig": {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": 24000,
            "speakingRate": rate
        }
    }
    return {
        "method": "POST",
        "url": endpoint + "/v1/text:synthesize?key=" + quote(secret, safe=''),
        "headers": {
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        "body": json.dumps(body).encode('utf-8')
    }

def _alignment_words(alignment):
    if not alignment:
        return []
    if not isinstance(alignment, dict):
        raise ValueError("Provider alignment must be an object")
    chars = alignment.get('characters', [])
    starts = alignment.get('character_start_times_seconds', [])
    ends = alignment.get('character_end_times_seconds', [])
    if not all(isinstance(values, list) for values in (chars, starts, ends)):
        raise ValueError("Provider alignment fields must be arrays")
    if len(chars) != len(starts) or len(chars) != len(ends):
        raise ValueError("Alignment arrays must have equal lengths")
    for start,end in zip(starts,ends):
        if not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or not math.isfinite(start) or not math.isfinite(end) or start<0 or end<start:
            raise ValueError('Invalid provider word timing')
    for i in range(1, len(starts)):
        if starts[i] < starts[i-1]:
            raise ValueError("Start times must be monotonic")
    for i in range(1, len(ends)):
        if ends[i] < ends[i-1]:
            raise ValueError("End times must be monotonic")
    if not all(isinstance(ch,str) for ch in chars):raise ValueError('Provider alignment characters must be text')
    text=''.join(chars)
    positions=[]
    for i,ch in enumerate(chars):
        if not isinstance(ch,str):raise ValueError('Provider alignment characters must be text')
        positions.extend([i]*len(ch))
    words=[]
    for match in re.finditer(r"[^\W_]+(?:['’][^\W_]+)*",text,re.UNICODE):
        words.append({'word':match[0],'start':starts[positions[match.start()]],'end':ends[positions[match.end()-1]]})
    return words

def parse_response(engine, data, timestamps=False):
    if engine=='fish' and timestamps:
        from fish_stream import parse_fish_sse
        return parse_fish_sse(data)
    if engine in ('fish', 'openai', 'azure'):
        if not data:raise ValueError('Provider returned empty audio')
        return data, []
    if engine in ('google', 'elevenlabs'):
        provider, key = ('Google', 'audioContent') if engine == 'google' else ('ElevenLabs', 'audio_base64')
        try:
            payload = json.loads(data.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError(f"Invalid JSON response from {provider}") from None
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid recording response from {provider}")
        audio_b64 = payload.get(key)
        if not isinstance(audio_b64, str) or not audio_b64:
            raise ValueError(f"Missing or invalid {key} in {provider} response")
        audio_bytes = base64.b64decode(audio_b64, validate=True)
        if not audio_bytes:
            raise ValueError(f"Empty audio from {provider}")
        alignment = (payload.get('alignment') or payload.get('normalized_alignment')) if engine == 'elevenlabs' else None
        return audio_bytes, _alignment_words(alignment)
    raise ValueError(f"Unknown engine: {engine}")

def build_request(profile, text, instructions, rate, voice, secret, reference=None):
    _validate_rate(rate)
    engine = profile.get('engine', '').lower()
    if engine == 'fish':
        return _build_fish(profile, text, instructions, rate, voice, secret, reference)
    elif engine == 'elevenlabs':
        return _build_elevenlabs(profile, text, instructions, rate, voice, secret)
    elif engine == 'openai':
        return _build_openai(profile, text, instructions, rate, voice, secret)
    elif engine == 'azure':
        return _build_azure(profile, text, instructions, rate, voice, secret)
    elif engine == 'google':
        return _build_google(profile, text, instructions, rate, voice, secret)
    else:
        raise ValueError(f"Unsupported engine: {engine}")
