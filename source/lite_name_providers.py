"""Desktop provider contracts for contextual WoW player-name takes."""

from base64 import b64encode
from contextlib import nullcontext
from hashlib import sha256
from http.client import IncompleteRead
from io import BytesIO
import json
import re
from ssl import SSLEOFError
import subprocess
import wave
from time import perf_counter
from urllib.parse import urlsplit
from urllib.request import Request

import soundfile as sf

from fish_references import joined_fish_reference, reference_budget
from http_protocols import build_request, parse_response


class UnusableProviderAudio(ValueError):
    """The provider responded, but its recording could not be used."""


ENGINES = {
    "fish": "Fish Audio (free)",
    "fish_omni": "Self-hosted Fish (SSH)",
    "elevenlabs": "ElevenLabs",
    "openai": "OpenAI",
    "azure": "Azure Speech",
    "google": "Google Cloud Text-to-Speech",
    "polly": "Amazon Polly",
}
ENDPOINTS = {
    "fish": "https://api.fish.audio",
    "elevenlabs": "https://api.elevenlabs.io",
    "openai": "https://api.openai.com",
    "google": "https://texttospeech.googleapis.com",
}
FREE_MODEL = "s2.1-pro-free"
MAX_RESPONSE = 8_000_000
RESPONSE_BODY_DEADLINE = 120


def read_provider_audio(response):
    deadline = perf_counter() + RESPONSE_BODY_DEADLINE
    audio = bytearray()
    try:
        while len(audio) <= MAX_RESPONSE:
            chunk = response.read1(min(64 * 1024, MAX_RESPONSE + 1 - len(audio)))
            if perf_counter() >= deadline:
                raise UnusableProviderAudio("The provider took too long to finish returning the recording.")
            if not chunk:
                break
            audio.extend(chunk)
    except (IncompleteRead, TimeoutError, ConnectionError, SSLEOFError) as error:
        raise UnusableProviderAudio("The provider returned an incomplete recording.") from error
    if len(audio) > MAX_RESPONSE:
        raise UnusableProviderAudio("The provider recording exceeds the response size limit.")
    length = response.headers.get("Content-Length")
    if (length and not response.headers.get("Transfer-Encoding")
            and length.isascii() and length.isdecimal() and str(len(audio)) != (length.lstrip("0") or "0")):
        raise UnusableProviderAudio("The provider returned an incomplete recording.")
    return bytes(audio)


def lossless_fish_body(body):
    """Compress the inline recording for transport, preserving every sample."""
    import msgpack
    from reference_media import pack_wave, unpack_wave

    fields = msgpack.unpackb(body, raw=False)
    reference, = fields["references"]
    original = reference["audio"]
    try:
        media, flac = pack_wave(original)
        if len(flac) >= len(original) or unpack_wave(media, flac) != original:
            return body
    except (ValueError, sf.LibsndfileError):
        # Fall back before a request is sent if the local codec cannot encode it.
        return body
    reference["audio"] = flac
    return msgpack.packb(fields, use_bin_type=True)


class NameProvider:
    """Snapshot one configured connection. The credential stays in memory."""

    def __init__(self, connections, ident, *, submit, allow_metered, timestamps, transport=None,
                 lossless_references=False):
        self.profile = dict(connections.profile(ident))
        self.saved_profile = dict(self.profile)
        self.engine = self.profile.get("engine")
        if self.engine not in ENGINES:
            raise ValueError("Choose a supported player-name provider.")
        self.label = ENGINES[self.engine]
        self.shared = None
        if self.engine == 'fish_omni' and self.profile.get('transport') == 'local':
            from lite_shared_fish import SharedFish
            self.shared = SharedFish(self.profile, ident)
            self.label = 'Shared local Fish'
        expected_transport = "ssh" if self.engine == "fish_omni" else "cloud"
        if not self.shared and self.profile.get("transport") != expected_transport:
            raise ValueError("Choose the required connection transport for this provider.")
        self.connections = connections
        self.ident = ident
        self.transport = transport
        self.lossless_references = lossless_references and self.engine == "fish"
        self.last_transfer = None
        self.timed = timestamps and self.engine == "fish"
        self.voice_map = {}
        self.profile["capabilities"] = "speed" if self.engine == "fish" else ""
        if self.engine in ENDPOINTS:
            self.profile["endpoint"] = ENDPOINTS[self.engine]
        elif self.engine == "azure":
            region = self.profile.get("region", "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9]{1,30}", region):
                raise ValueError("Enter the Azure Speech resource region.")
            self.profile["endpoint"] = f"https://{region}.tts.speech.microsoft.com"
        if self.engine == "fish":
            if self.profile.get("model") != FREE_MODEL:
                raise ValueError("Lite requires the exact free Fish model.")
            self.profile["syntax"] = "brackets"
            if self.timed:
                self.profile["capabilities"] += ",timestamps"
        elif self.engine == "fish_omni" and not self.shared:
            self.profile.update(model="drama-3", syntax="brackets")
            host = self.profile.get("ssh_host", "")
            remote = self.profile.get("remote_host", "")
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", host) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", remote):
                raise ValueError("Enter a valid SSH destination and remote TTS address.")
            for key in ("ssh_port", "remote_port"):
                field = self.profile.get(key, "")
                if not field.isascii() or not field.isdecimal() or not 1 <= int(field) <= 65535:
                    raise ValueError("Enter SSH and TTS ports between 1 and 65535.")
        elif self.engine not in {'fish', 'fish_omni'}:
            self.profile["syntax"] = "plain"
            for line in self.profile.get("npc_voices", "").splitlines():
                if not line.strip():
                    continue
                npc, equals, voice = line.partition("=")
                npc, voice = npc.strip(), voice.strip()
                if (not equals or not re.fullmatch(r"[1-9][0-9]*", npc)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", voice) or npc in self.voice_map):
                    raise ValueError("Enter one unique NPC ID = provider voice ID per line.")
                self.voice_map[npc] = voice
            if submit and not self.voice_map:
                raise ValueError("This provider needs a matching NPC voice before it can fetch names.")
            if submit and not allow_metered:
                raise ValueError("Confirm use of the selected provider’s potentially metered account.")
        self.model = self.profile.get("model", "").strip()
        if self.engine in {"openai", "elevenlabs"} and not self.model:
            raise ValueError("Enter the provider’s speech model ID.")
        if self.engine == "google":
            self.quota_project = self.profile.get("quota_project", "").strip()
            if self.quota_project and not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", self.quota_project):
                raise ValueError("Enter a valid Google Cloud quota project ID.")
        if self.engine == "polly":
            region = self.profile.get("region", "").strip()
            if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
                raise ValueError("Enter the AWS region for Amazon Polly.")
            if not self.profile.get("aws_profile", "").strip():
                raise ValueError("Enter the name of an existing AWS profile.")
            if self.model not in {"standard", "neural", "long-form", "generative"}:
                raise ValueError("Enter a supported Polly engine.")
        self.secret = (connections.secret(ident, self.profile)
                       if submit and self.engine not in {"polly", "fish_omni"} else "")
        if submit and self.engine not in {"polly", "fish_omni"} and not self.secret.strip():
            raise ValueError(f"Save the {self.label} credential before fetching names.")
        if self.secret and (len(self.secret) > 8192 or not self.secret.isprintable()):
            raise ValueError("Paste only the provider token or key.")

    @property
    def raw_name(self):
        if self.shared:
            return 'fish-raw.wav'
        if self.engine == "fish":
            return "fish-raw.sse" if self.timed else "fish-raw.wav"
        return "provider-raw.bin"

    def request(self, text, npc_id, reference):
        if self.engine in {"fish", "fish_omni"}:
            reference_budget(reference)
        if self.engine == "fish":
            if isinstance(reference, list) and len(reference) > 1:
                joined, evidence, _ = joined_fish_reference(reference)
                wire = build_request(self.profile, text, "", 1.0, "", self.secret, reference=[joined])
                wire["reference_evidence"] = evidence
            else:
                wire = build_request(self.profile, text, "", 1.0, "", self.secret, reference=reference)
            route = "/v1/tts/stream/with-timestamp" if self.timed else "/v1/tts"
            if (wire["url"], wire["headers"]["model"], wire["headers"]["Content-Type"]) != (
                    ENDPOINTS["fish"] + route, FREE_MODEL, "application/msgpack"):
                raise ValueError("The request does not use the exact free Fish API route.")
            return wire
        if self.engine == "fish_omni":
            return self.ssh_request(text, reference)
        if "[" in text or "]" in text:
            raise ValueError("This provider needs a mapped equivalent for the dialogue’s speech directions.")
        voice = self.voice_map.get(str(npc_id))
        if not voice:
            raise ValueError(f"No matching provider voice is configured for NPC {npc_id}.")
        if self.engine == "polly":
            args = dict(Text=text, VoiceId=voice, Engine=self.model, OutputFormat="mp3")
            region = self.profile["region"]
            return dict(url=f"https://polly.{region}.amazonaws.com/v1/speech",
                        headers={}, body=json.dumps(args, sort_keys=True).encode(), voice=voice)
        wire = build_request(self.profile, text, "", 1.0, voice, self.secret)
        wire["voice"] = voice
        if self.engine == "openai" and voice.startswith("voice_"):
            body = json.loads(wire["body"])
            body["voice"] = {"id": voice}
            wire["body"] = json.dumps(body).encode()
        if self.engine == "elevenlabs" and self.model == "eleven_v3":
            body = json.loads(wire["body"])
            del body["voice_settings"]["use_speaker_boost"]
            wire["body"] = json.dumps(body).encode()
        if self.engine == "google":
            body = json.loads(wire["body"])
            locale = re.match(r"([a-z]{2,3}-(?:[A-Z]{2}|[0-9]{3}))-", voice)
            if locale:
                body["voice"]["languageCode"] = locale[1]
            if re.search(r"-Chirp(?:3)?-HD-", voice):
                del body["audioConfig"]["speakingRate"]
            wire["body"] = json.dumps(body).encode()
            wire["url"] = self.profile["endpoint"] + "/v1/text:synthesize"
            wire["headers"]["Authorization"] = "Bearer " + self.secret
            if self.quota_project:
                wire["headers"]["x-goog-user-project"] = self.quota_project
        if self.engine == "azure":
            wire["headers"]["User-Agent"] = "DMSpeaks-Lite"
        return wire

    def ssh_request(self, text, reference):
        joined, evidence, reference_bytes = joined_fish_reference(reference)
        attachment = b64encode(joined["wav_bytes"]).decode()
        body = dict(model="drama-3", input=text, ref_audio="data:audio/wav;base64,"+attachment,
                    ref_text=joined["transcript"], seed=37,
                    response_format="wav", word_timestamps=True,
                    max_new_tokens=min(2048, max(512, 12*len(text.split()))))
        profile = self.profile
        origin = (profile['endpoint'] if self.shared else
                  f'ssh:{profile["ssh_host"]}:{profile["ssh_port"]}'
                  f'/{profile["remote_host"]}:{profile["remote_port"]}')
        return dict(url=origin+"/v1/audio/speech", headers={"Content-Type":"application/json","Accept":"audio/wav"},
                    body=json.dumps(body, ensure_ascii=False).encode(), reference_bytes=reference_bytes,
                    reference_evidence=evidence)

    def identity(self, wire, reference_evidence):
        # Secrets never enter a receipt or cache identity. Exact reference and
        # contextual request bind each take even for enrolled-voice providers.
        request = {
            "engine": self.engine,
            "endpoint": wire["url"],
            "voice": wire.get("voice"),
            "model": self.model,
            "reference": reference_evidence,
            "body_sha256": sha256(wire["body"]).hexdigest(),
        }
        return sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()

    def submission(self, request_id, source_id):
        self.verify_profile()
        return self.shared.submission(request_id, source_id) if self.shared else nullcontext()

    def verify_profile(self):
        if self.shared and dict(self.connections.profile(self.ident)) != self.saved_profile:
            raise ValueError('The saved shared Fish connection changed while this request was queued. Review the selected profile.')

    def obtain(self, wire, opener, *, on_send=None):
        self.last_transfer = None
        if self.shared:
            # A transport injection must not bypass the real shared lease.
            self.verify_profile()
            self.shared.verify_submission()
        if self.transport is not None:
            if on_send:
                on_send()
            return self.transport(wire)
        if self.engine == "polly":
            try:
                import boto3
                from botocore.config import Config
                from botocore.exceptions import BotoCoreError, ClientError
                from urllib3.exceptions import ProtocolError, ReadTimeoutError
            except ImportError:
                raise ValueError("Amazon Polly needs the desktop AWS SDK.") from None
            try:
                client = boto3.Session(profile_name=self.profile["aws_profile"], region_name=self.profile["region"])
                limits = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
                speech = client.client("polly", config=limits)
                arguments = json.loads(wire["body"])
                if on_send:
                    on_send()
                result = speech.synthesize_speech(**arguments)
                with result["AudioStream"] as stream:
                    try:
                        return read_provider_audio(stream)
                    except (ProtocolError, ReadTimeoutError) as error:
                        raise UnusableProviderAudio("The provider returned an incomplete recording.") from error
            except ClientError as exc:
                from urllib.error import HTTPError
                code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 400)
                raise HTTPError(wire["url"], code, "Provider rejected synthesis.", {}, None) from None
            except BotoCoreError:
                raise OSError("The AWS profile or provider connection could not be used.") from None
        url = wire["url"]
        if self.shared:
            from lite_shared_fish import ENDPOINT
            if url != ENDPOINT + '/v1/audio/speech':
                raise ValueError('The shared Fish request changed its approved endpoint.')
        elif self.engine == "fish_omni":
            self.verify_ssh_target()
            tunnel = self.connections.endpoint(self.profile)
            address = urlsplit(tunnel)
            if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
                    or address.path or address.query or address.username or address.password):
                raise ValueError("The SSH connection did not provide a trusted loopback forward.")
            url = tunnel + "/v1/audio/speech"
        elif urlsplit(url).scheme != "https":
            raise ValueError("The provider connection must use HTTPS.")
        body = lossless_fish_body(wire["body"]) if self.lossless_references else wire["body"]
        request = Request(url, data=body, headers=wire["headers"], method="POST")
        started = perf_counter()
        if on_send:
            on_send()
        if self.engine == "fish_omni":
            from fish_community_tts import open_loopback
            opened = open_loopback(request, timeout=60)
        else:
            opened = opener.open(request, timeout=60)
        received_headers = perf_counter()
        with opened as response:
            if self.engine == "fish_omni" and response.headers.get("X-Audio-Truncated", "").strip().lower() == "true":
                raise UnusableProviderAudio("Self-hosted Fish returned an incomplete recording.")
            audio = read_provider_audio(response)
        if self.lossless_references:
            self.last_transfer = {
                "request_body_sha256": sha256(body).hexdigest(),
                "request_bytes": len(body),
                "reference_format": "wav" if body is wire["body"] else "flac",
                "response_headers_seconds": round(received_headers - started, 3),
                "response_body_seconds": round(perf_counter() - received_headers, 3),
            }
        return audio

    def verify_ssh_target(self):
        import socket
        host = self.profile["ssh_host"].rsplit("@", 1)[-1].casefold()
        target = host.split(".", 1)[0]
        machine = socket.gethostname().split(".", 1)[0].casefold()
        # Retain the managed-host denials without embedding its network identifiers.
        guarded_name = sha256(target.encode()).hexdigest() == "a8c62b2e5a806548722f9509f1dc5f29adea36b5d6831fead10aa44607a10a3f"
        guarded_address = sha256(host.encode()).hexdigest() == "04d1f7974020482f9cd7844bbbd4dfb9b8c21e8e4b256745e8b079f4f53d4d9d"
        if (target in {"spark", "localhost"} or host in {"127.0.0.1", "::1"}
                or guarded_name or guarded_address or machine.startswith("spark") and target == machine):
            raise ValueError("Choose a self-hosted Fish server that accepts direct SSH connections.")

    def normalized_audio(self, raw):
        if self.engine == "polly":
            audio = raw
        else:
            audio, _ = parse_response(self.engine, raw)
        file_format = "mp3" if self.engine in {"elevenlabs", "polly"} else "wav"
        # Limit output so malformed or unexpectedly long returns are rejected
        # before they can enter the name assembly or exhaust memory.
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", file_format, "-i", "pipe:0",
             "-map", "0:a:0", "-ac", "1", "-ar", "44100", "-t", "61",
             "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
            input=audio, capture_output=True, timeout=30, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        pcm = result.stdout
        if result.returncode or not 0 < len(pcm) <= 60 * 44100 * 2 or len(pcm) % 2:
            raise ValueError("The provider returned incomplete or unsupported audio.")
        with BytesIO() as out:
            with wave.open(out, "wb") as wav:
                wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
                wav.writeframes(pcm)
            return out.getvalue()
