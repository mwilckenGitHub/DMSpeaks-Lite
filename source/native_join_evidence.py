"""Bind native join evidence to saved media and carry it across project opens."""

import hashlib
import json
from pathlib import Path
import re

EVIDENCE_LIMIT = 2 * 1024 * 1024


def context_media(piece):
    native = piece.get('native_edit_provenance')
    context = native.get('full_context') if isinstance(native, dict) else None
    window = piece.get('native_context_window')
    return context if isinstance(context, dict) and isinstance(window, dict) else None


def context_path(store, piece):
    context = context_media(piece) or {}
    uri = context.get('audio_uri')
    if not isinstance(uri, str) or not uri:
        return None
    path = Path(uri).resolve()
    return path if path.is_relative_to(store.folder.resolve()) and path.is_file() else None


def evidence_path(store, piece):
    identity = piece.get('sha256')
    if not (_is_digest(identity) and context_media(piece)):
        return None
    return store.folder / 'join-evidence' / (identity + '.json')


def available(store, piece):
    path = evidence_path(store, piece)
    return bool(path and context_path(store, piece) and (
        path.is_file() or context_media(piece).get('join_evidence') or
        piece['native_edit_provenance'].get('raw_timestamps_header')))


def saved_context_available(store, piece):
    if context_path(store, piece) is None:
        return False
    try:
        read(store, piece)
    except ValueError:
        return False
    return True


def _is_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _read_evidence(store, piece):
    stored = (context_media(piece) or {}).get('join_evidence')
    if stored is not None:
        return _validate_evidence(stored, piece)
    path = evidence_path(store, piece)
    if path is None or not path.is_file():
        raise ValueError('No saved join evidence is available for this recording.')
    with path.open('rb') as file:
        raw = file.read(EVIDENCE_LIMIT + 1)
    if len(raw) > EVIDENCE_LIMIT:
        raise ValueError('The saved join evidence is too large.')
    try:
        evidence = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('The saved join evidence could not be read.') from None
    return _validate_evidence(evidence, piece)


def _validate_evidence(evidence, piece):
    if (not isinstance(evidence, dict) or evidence.get('format') != 'dmspeaks-native-join-evidence-1'
            or not _is_digest(piece.get('sha256'))
            or evidence.get('insert_sha256') != piece['sha256']
            or evidence.get('replacement') != piece.get('insert_expected_text')):
        raise ValueError('The saved join evidence belongs to a different replacement.')
    for field in ('source_evidence', 'context_evidence'):
        if not isinstance(evidence.get(field), dict) or not isinstance(evidence[field].get('words'), list):
            raise ValueError('The saved join evidence needs timed words for both recordings.')
    if evidence.get('recognized_as') is not None and not isinstance(evidence['recognized_as'], str):
        raise ValueError('The saved recognition spelling must be text.')
    if evidence.get('timing_basis') not in (None, 'estimated_supplied_transcript',
                                           'estimated_phoneme_alignment'):
        raise ValueError('The saved timing source is invalid.')
    if 'insert_evidence' in evidence:
        insert = evidence['insert_evidence']
        if (not isinstance(insert, dict) or insert.get('audio_sha256') != piece['sha256']
                or not isinstance(insert.get('words'), list)):
            raise ValueError('Saved isolated word evidence belongs to a different return.')
    context_digest = (context_media(piece) or {}).get('sha256')
    source_digest = piece['native_context_window'].get('source_sha256')
    if (not _is_digest(context_digest) or not _is_digest(source_digest)
            or evidence['context_evidence'].get('audio_sha256') != context_digest
            or evidence['source_evidence'].get('audio_sha256') != source_digest):
        raise ValueError('Saved join evidence refers to different source or context audio.')
    return evidence


def read(store, piece):
    evidence = _read_evidence(store, piece)
    return {key: evidence[key] for key in ('source_evidence', 'context_evidence', 'recognized_as',
                                           'insert_evidence', 'timing_basis')
            if key in evidence}


def set_evidence(piece, evidence):
    _validate_evidence(evidence, piece)
    if len(json.dumps(evidence, ensure_ascii=False).encode()) > EVIDENCE_LIMIT:
        raise ValueError('The saved join evidence is too large.')
    provenance = piece['native_edit_provenance']
    return {**piece, 'native_edit_provenance': {**provenance,
            'full_context': {**provenance['full_context'], 'join_evidence': evidence}}}


def timeline_word_asset(layer, asset):
    """Return the word clock that belongs to this layer's unchanged media."""
    timing = layer.get('native_join_timing')
    digest = asset.get('sha256')
    if not isinstance(timing, dict) or not _is_digest(digest) or timing.get('audio_sha256') != digest:
        return asset
    from alignment_backend import validate_words

    words = timing.get('words')
    source = timing.get('timing_source')
    if (not isinstance(source, str) or not isinstance(words, list) or
            any(not isinstance(word, dict) or not isinstance(word.get('word'), str) for word in words)):
        raise ValueError('Saved join word timings are invalid.')
    validate_words(words, words, asset['sample_count'] / asset['sample_rate'])
    return {**asset, 'words': words, 'timing_source': source or 'unavailable'}


def bundle_context(archive, item, asset, store, included):
    """Write an owned codec decode once, without exporting its local filename."""
    context = context_media(asset)
    if context is None:
        return
    portable = {key: value for key, value in context.items()
                if key not in {'audio_uri', 'media_entry', 'join_evidence'}}
    item['native_edit_provenance'] = {**asset['native_edit_provenance'], 'full_context': portable}
    path = context_path(store, asset)
    if path is None:
        return  # Older projects may have the clip but no retained codec decode.
    digest = context.get('sha256')
    with path.open('rb') as file:
        if not _is_digest(digest) or hashlib.file_digest(file, 'sha256').hexdigest() != digest:
            raise ValueError('The saved native return context changed. Reopen the correct recording.')
    entry = f'native-returns/{digest}.wav'
    if entry not in included:
        archive.write(path, entry)
        included.add(entry)
    portable['media_entry'] = entry
    try:
        portable['join_evidence'] = _read_evidence(store, asset)
    except ValueError:
        pass  # Optional recognition can be absent even with a valid codec return.


def portable_media(asset, entries):
    """Validate the manifest reference before the project reads or writes media."""
    context = context_media(asset)
    if context is None or 'media_entry' not in context:
        return None
    digest = context.get('sha256')
    entry = context['media_entry']
    if not (_is_digest(digest) and entry == f'native-returns/{digest}.wav' and entry in entries
            and context.get('sample_rate') == 44100 and context.get('channels') == 1
            and type(context.get('sample_count')) is int and 0 < context['sample_count'] <= 44100 * 1800):
        raise ValueError('Invalid saved native return context in this project.')
    if 'join_evidence' in context:
        evidence = _validate_evidence(context['join_evidence'], asset)
        if len(json.dumps(evidence, ensure_ascii=False).encode()) > EVIDENCE_LIMIT:
            raise ValueError('The saved join evidence is too large.')
    return context


def restore_context(asset, validated_media, folder):
    context = context_media(asset)
    if context is None:
        return
    context.pop('audio_uri', None)  # A portable project never grants access to an external path.
    entry = context.pop('media_entry', None)
    if entry is None:
        context.pop('join_evidence', None)
        return
    folder = folder / 'native-returns'
    folder.mkdir(exist_ok=True)
    target = folder / Path(entry).name
    try:
        file = target.open('xb')
    except FileExistsError:
        with target.open('rb') as file:
            if hashlib.file_digest(file, 'sha256').hexdigest() != context['sha256']:
                raise ValueError('Existing native return context has changed.') from None
    else:
        try:
            with file:
                file.write(validated_media[entry])
        except OSError:
            target.unlink(missing_ok=True)
            raise
    context['audio_uri'] = str(target)
