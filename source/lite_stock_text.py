"""Authenticate individual captions in edited Spoken release tables without evaluating Lua."""
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re


MAX_TABLE_BYTES = 8 * 1024 * 1024
_PIN_FILE = Path(__file__).with_name("formats") / "spoken-caption-digests.json"
_PIN_FILE_SHA256 = "3abc6f0ef4b084eb526be0c7c25f532365d94bdd5ebee0799c871a1434c39f08"
_GUARDS = (
    b"if not VoiceOver or not VoiceOver.DataModules then return end",
    b'if not VoiceOver.Language or VoiceOver.Language:GetClientLanguage() ~= "enUS" then return end',
)
# This is the data syntax written by Spoken's exporter. Anchoring each whole
# line also rules out trailing statements, expressions, and alternate keys.
_ENTRY = re.compile(rb'\["([A-Za-z0-9_-]{1,100})"\][ \t]*=[ \t]*"((?:[^"\\\x00-\x1f]|\\(?:[\\\"nrt]|[0-9]{3}))*)"[ \t]*')
_ESCAPE = re.compile(rb'\\([\\\"nrt]|[0-9]{3})')
_LITERALS = {b'"': b'"', b"\\": b"\\", b"n": b"\n", b"r": b"\r", b"t": b"\t"}


def _unescape(match):
    token = match[1]
    if len(token) == 3:
        return bytes((int(token),))
    return _LITERALS[token]


def generated_captions(content, pack):
    """Read a single, bounded TextByFileName literal with the enUS guards."""
    if len(content) > MAX_TABLE_BYTES or content.count(b"\n") > 65536:
        raise ValueError("The installed Spoken text table is too large.")
    # Split only on Lua's physical newlines. Some Unicode line separators are
    # ordinary text inside a quoted caption.
    lines = [line for raw in content.replace(b"\r\n", b"\n").split(b"\n")
             if (line := raw.strip(b" \t"))]
    header = (*_GUARDS, pack.encode("ascii") + b".TextByFileName = {")
    if tuple(lines[:3]) != header or lines[-1:] != [b"}"]:
        raise ValueError("The installed Spoken text table has an unfamiliar format.")
    captions = {}
    for index, line in enumerate(lines[3:-1], start=3):
        if index != len(lines) - 2 and not line.endswith(b","):
            raise ValueError("The installed Spoken text table is missing an entry separator.")
        entry = _ENTRY.fullmatch(line.removesuffix(b",")) if len(line) <= 64 * 1024 else None
        if entry is None:
            raise ValueError("The installed Spoken text table contains an unsupported entry.")
        key = entry[1].decode("ascii")
        caption = _ESCAPE.sub(_unescape, entry[2]).decode("utf-8")
        # A repeated literal key cannot establish which take was intended.
        # Other keys in this complete, data-only table retain their meaning.
        captions[key] = None if key in captions else caption
    return captions


@lru_cache(maxsize=4)
def _edited_captions(content, pack):
    # Keep a rejected table in the cache too. A coverage pass may check
    # hundreds of recordings from the same malformed table.
    try:
        return generated_captions(content, pack)
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _release_pins():
    try:
        content = _PIN_FILE.read_bytes()
    except OSError:
        return {}
    if sha256(content).hexdigest() != _PIN_FILE_SHA256:
        return {}
    return json.loads(content)


def matches_edited_table(content, pack, table_digest, key, expected_caption):
    """Require agreement between the installed row, the plan and the release."""
    release = _release_pins().get(table_digest)
    if not release or release["pack"] != pack or not isinstance(expected_caption, str):
        return False
    captions = _edited_captions(content, pack)
    if captions is None or captions.get(key) != expected_caption:
        return False
    return release["captions"].get(key) == sha256(expected_caption.encode("utf-8")).hexdigest()
