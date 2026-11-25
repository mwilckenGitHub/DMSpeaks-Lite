"""Explicit identities for signed Spoken and corpus-native companion sources."""

from hashlib import sha256
import json
import re


def dialogue_source_tuple(manifest):
    return tuple(manifest.get(key) for key in ("source_id", "npc_id", "locale"))


def quest_wording_key(manifest):
    """Identify authored quest variants using the playback whitespace rules."""
    source = manifest.get("source_id")
    if not isinstance(source, str) or not source.startswith("q:"):
        return None
    route = manifest.get("native_route") or {}
    caption = manifest.get("reconstructed_caption") or {}
    text = caption.get("quest_text") or route.get("original_text") or manifest.get("original_text")
    if not isinstance(text, str):
        return None
    normalized = re.sub(r"[ \t\r\n\f\v]+", " ", text).strip(" ")
    return sha256(normalized.encode()).hexdigest() if normalized else None


def source_key(row):
    if row.get("quest_variant"):
        identity = (*dialogue_source_tuple(row), row["source_text_sha256"])
        key = sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return f"quest-variant:{key}"
    native=row.get('native_source')
    return 'native:'+native['source_contract_sha256'] if native else row['stock_path']


def native_identity(row):
    native=row.get('native_source')
    return {'source_contract_sha256':native['source_contract_sha256']} if native else None


def native_filters(row):
    return {key:row[key] for key in ('races','classes','sexes') if key in row}


def signed_binding(row):
    return None if row.get('native_source') else dict(path=row['stock_path'],sha256=row['stock_sha256'])


def manifest_key(manifest):
    if manifest.get('paced_source'):
        return 'paced:'+manifest['paced_source']['native_manifest_sha256']
    if manifest.get('native_source') or manifest.get("quest_variant"):
        return source_key(manifest)
    from lite_portable_io import stock_binding
    binding=stock_binding(manifest)
    return binding['path'] if binding else None


def source_directory(row):
    if row.get('native_source'):
        kind=row['native_route']['kind']
        if kind not in {'quest', 'gossip', 'followup'}:
            raise ValueError('The native source needs its explicit event adapter.')
        return {'quest': 'quests', 'gossip': 'gossip', 'followup': 'followup'}[kind]
    return row['stock_path'].split('/')[-2]
