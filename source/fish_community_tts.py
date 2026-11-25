"""Community vLLM-Omni Fish speech API; local Qwen draft reviewed and corrected.

The original speaker reference and exact prompt are retained. Returned attention
word times are approximate labels, never validated acoustic decay boundaries.
"""
import base64
import hashlib
import json
import mimetypes
import re
import socket
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOOPBACK_RE = re.compile(r"^(127\.0\.0\.1|localhost|\[?::1\]?)$", re.IGNORECASE)


def open_loopback(request: urllib.request.Request, timeout: float):
    from providers import _NoCheckRedirects

    # Local Fish and SSH forwards carry voice references over a trusted loopback connection.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoCheckRedirects)
    return opener.open(request, timeout=timeout)


def _validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("endpoint must be a non-empty string")
    try:
        parsed = urllib.parse.urlparse(endpoint)
    except Exception as exc:
        raise ValueError(f"invalid endpoint URL: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError("endpoint must use http or https scheme")
    if parsed.username or parsed.password:
        raise ValueError("endpoint must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("endpoint must not contain query parameters")
    host = parsed.hostname or ""
    if not _LOOPBACK_RE.match(host):
        raise ValueError("endpoint host must be 127.0.0.1, localhost, or ::1")
    if parsed.port is None:raise ValueError("Include the community runtime port.")
    return endpoint.rstrip("/")


def _read_reference(reference: Dict[str, Any]) -> Tuple[bytes, str, str]:
    if not isinstance(reference, dict):
        raise ValueError("reference must be a dict")
    path = reference.get("path")
    transcript = reference.get("transcript")
    ref_id = reference.get("id")
    if not isinstance(path, str) or not path:
        raise ValueError("reference.path must be a non-empty string")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("reference.transcript must be a non-empty string")
    if not isinstance(ref_id, str) or not ref_id:
        raise ValueError("reference.id must be a non-empty string")
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"reference file not found: {p}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read reference file: {exc}") from exc
    if not data:
        raise ValueError("reference file is empty")
    return data, transcript, ref_id


def _mime_for_path(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "audio/wav"


def _normalize_timestamps(raw: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not isinstance(raw, list):
        return [], f"malformed x-word-timestamps: expected list, got {type(raw).__name__}"
    words: List[Dict[str, Any]] = []
    prev_end = -1.0
    for item in raw:
        if not isinstance(item, dict):
            return [], f"malformed x-word-timestamps: item not dict: {item!r}"
        word = item.get("word")
        start_ms = item.get("start_ms")
        end_ms = item.get("end_ms")
        if not isinstance(word, str) or not word:
            return [], f"malformed x-word-timestamps: invalid word: {word!r}"
        try:
            start_s = float(start_ms) / 1000.0
            end_s = float(end_ms) / 1000.0
        except (TypeError, ValueError):
            return [], f"malformed x-word-timestamps: non-numeric times: {start_ms!r}, {end_ms!r}"
        if isinstance(start_ms,bool) or isinstance(end_ms,bool) or not (math.isfinite(start_s) and math.isfinite(end_s) and start_s >= 0 and start_s <= end_s):
            return [], f"malformed x-word-timestamps: invalid range: {start_s}, {end_s}"
        if start_s < prev_end:
            return [], f"malformed x-word-timestamps: out of order: {end_s} < {prev_end}"
        prev_end = end_s
        words.append({"word": word, "start": start_s, "end": end_s})
    return words, None


def _extract_metadata(headers: Dict[str, str]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    for key, value in headers.items():
        lk = key.lower()
        if lk.startswith("x-vllm-omni-") or lk.startswith("x-audio-"):
            meta[key] = value
    return meta


def fit_final_word_end(words, duration):
    """Reconcile a millisecond-rounded final label with the decoded audio end."""
    if not words:
        return words
    last = words[-1]
    rounded_duration = round(duration * 1000) / 1000
    if last['start'] < duration < last['end'] == rounded_duration:
        return [*words[:-1], {**last, 'end': duration}]
    return words


def has_attention_header_clock(asset):
    """Identify the community Fish HTTP clock on a recorded take."""
    provider = asset.get("provider") or {}
    return (asset.get("timing_source") == "estimated_community_fish_attention"
            and provider.get("runtime") == "community-vllm-omni"
            and bool(provider.get("raw_timestamps_header")))


def restore_attention_frame_time(seconds):
    """Recover an exact codec boundary from an identifiable millisecond bin."""
    milliseconds = round(seconds * 1000)
    if seconds != milliseconds / 1000:
        return seconds
    frame = round(milliseconds * 44100 / (1000 * 2048))
    samples = frame * 2048
    if round(samples * 1000 / 44100) != milliseconds:
        return seconds
    return samples / 44100


def generate(
    endpoint: str,
    text: str,
    reference: Dict[str, Any],
    output: Path,
    model: str = "drama-3",
    seed: int = 42,
    timeout: int = 1800,
    max_new_tokens: Optional[int] = None,
) -> Tuple[Path, List[Dict[str, Any]], Dict[str, Any]]:
    if type(seed) is not int:raise ValueError("Seed must be an integer.")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if not isinstance(output, Path):
        output = Path(output)
    if output.exists():
        raise ValueError(f"output path already exists: {output}")

    endpoint = _validate_endpoint(endpoint)
    ref_bytes, transcript, ref_id = _read_reference(reference)
    ref_sha256 = hashlib.sha256(ref_bytes).hexdigest()
    mime = _mime_for_path(str(reference.get("path", "")))
    b64 = base64.b64encode(ref_bytes).decode("ascii")

    payload: Dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": "wav",
        "word_timestamps": True,
        "seed": seed,
        "ref_audio": f"data:{mime};base64,{b64}",
        "ref_text": transcript,
    }
    if max_new_tokens is not None:
        if not isinstance(max_new_tokens, int) or isinstance(max_new_tokens, bool) or max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be a positive int if supplied")
        payload["max_new_tokens"] = max_new_tokens

    url = endpoint.rstrip("/") + "/v1/audio/speech"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        },
    )

    try:
        with open_loopback(req, timeout=timeout) as resp:
            status = resp.status
            if status != 200:
                raise ValueError(f"unexpected HTTP status {status}")
            audio_bytes = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raise ValueError(f"HTTP error {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"network error: {exc.reason}") from exc
    except socket.timeout as exc:
        raise ValueError(f"timeout after {timeout}s") from exc
    except Exception as exc:
        raise ValueError(f"request failed: {exc}") from exc

    if not audio_bytes or len(audio_bytes) < 44 or (audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE"):
        raise ValueError("response is not a valid WAV file (missing RIFF header)")

    raw_ts_header = resp_headers.get("x-word-timestamps")
    words: List[Dict[str, Any]] = []
    ts_warning: Optional[str] = None
    if raw_ts_header is not None:
        try:
            raw_list = json.loads(raw_ts_header)
            words, ts_warning = _normalize_timestamps(raw_list)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            words = []
            ts_warning = f"malformed x-word-timestamps: {exc}"
    omission = resp_headers.get("x-word-timestamps-omitted", "").strip()[:256] if raw_ts_header is None else ""
    if omission:
        oversized = re.fullmatch(r"oversize; bytes=([0-9]{1,9}); limit=([0-9]{1,9})", omission)
        if oversized:
            size, limit = oversized.groups()
            ts_warning = f"Fish omitted word timestamps because the timing metadata exceeded the response limit ({size} bytes, limit {limit})."
        else:
            ts_warning = "Fish reported that it omitted word timestamps from this response."

    metadata: Dict[str, Any] = {
        "model": model,
        "seed": seed,
        "reference_id": ref_id,
        "reference_sha256": ref_sha256,
        "timestamp_source": "attention-derived approximate alignment",
        "timing_source":"estimated_community_fish_attention",
        "alignment":{"method":"community Fish attention word labels; acoustic decay not established","timestamp_resolution_seconds":2048/44100},
        "timestamp_warning": ts_warning,
        "raw_timestamps_header": raw_ts_header,
    }
    metadata.update(_extract_metadata(resp_headers))
    if omission:
        metadata["word_timestamps_omitted"] = omission

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as file:file.write(audio_bytes)

    # The patch can report style controls as word labels although they are not
    # spoken. Remove only exact leading control labels, retaining the raw header.
    prefix=re.match(r'^\s*(?:\[[^\]\n]+\]\s*)+',text)
    if prefix:
        tags=prefix.group().split()
        if [w['word'] for w in words[:len(tags)]]==tags:
            metadata['excluded_control_word_labels']=tags;words=words[len(tags):]
    if not words:
        metadata["timing_source"] = "unavailable"
        metadata["timestamp_source"] = None
        metadata.pop("alignment")
    return output, words, metadata


def check(endpoint: str) -> bool:
    endpoint = _validate_endpoint(endpoint)
    url = endpoint.rstrip("/") + "/health"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with open_loopback(req, timeout=30) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
            try:
                obj = json.loads(data)
                return obj.get("status") == "healthy"
            except (json.JSONDecodeError, AttributeError):
                return False
    except Exception:
        return False
