"""Make a saved Fish decode available to the normal contextual join planner."""

import hashlib
import json

from audio import decode, probe
from content_review import assert_audio_usable, media_digest
from engine_limits import MAX_AUDIO_SECONDS
from layered_audio import RATE
from native_edit_workflow import saved_context_audition_source
from native_join_evidence import context_media, context_path, read
from native_join_proposal import _locate, _tokens
from performance_plan import parse_record
from speech_edit_authoring import WORDS, delimiter_text_spans
from spoken_spans import normalized_spoken_spans
from timed_contextual_packets import _timed_words


def _generated_region(piece, pcm, insert_pcm):
    """Confirm the generated span against the retained insert samples."""
    native = piece['native_edit_provenance']
    begin, end = (native.get(key) for key in ('generated_start_sample_in_context',
                                              'generated_end_sample_in_context'))
    if (type(begin) is not int or type(end) is not int
            or not 0 <= begin < end <= len(pcm) or pcm[begin:end] != insert_pcm):
        raise ValueError('The saved Fish context does not match the returned clip.')
    return begin, end


def verified_cut_region(app, record, source, context, pcm):
    """Check the native source, insert, retained context, and saved timing together."""
    receipt = context.get('native_context_take')
    if not isinstance(receipt, dict) or not context.get('source_clip_id'):
        raise ValueError('This Fish return has no saved insert for boundary review.')
    _, original, piece, occurrence, _, _ = saved_context_audition_source(app, {
        'dataset': record['dataset'], 'index': record['index'], 'asset_id': context['source_clip_id']})
    timing_sha256 = hashlib.sha256(json.dumps(context['words'], sort_keys=True).encode()).hexdigest()
    provenance = context_media(piece) or {}
    generation = context['generation']
    segments = generation.get('segments') or []
    if (original['id'] != source['id'] or context.get('source_take_asset_id') != source['id']
            or piece.get('source_revision') != record['revision']
            or piece.get('source_metadata_revision') != record.get('metadata_revision')
            or piece['sha256'] != receipt.get('insert_sha256')
            or context['sha256'] != receipt.get('context_sha256')
            or context['sha256'] != provenance.get('sha256')
            or len(pcm) != provenance.get('sample_count')
            or occurrence['slot_order'] != generation.get('slot_order')
            or len(segments) != 1 or segments[0].get('value') != piece.get('value')
            or timing_sha256 != receipt.get('timing_sha256')):
        raise ValueError('The saved Fish source, insert or timing no longer matches this return.')
    assert_audio_usable(piece, app.store, audition_only=True)
    if media_digest(piece) != piece['sha256']:
        raise ValueError('The saved Fish insert changed. Review the recording again.')
    return _generated_region(piece, pcm, decode(piece['path']))


def _lexical_context(record, source, piece):
    generation = source['generation']
    segments = parse_record(record, source.get('player', {}), {
        'stage_directions': generation.get('stage_direction_format', 'auto'),
    })['segments']
    transcript, spans = normalized_spoken_spans(''.join(segment['resolved_text'] for segment in segments))
    occurrence = next(((segment, lo, hi) for segment, _, _, lo, hi in
                       delimiter_text_spans(segments, transcript, spans)
                       if segment['order'] == piece['slot_order']), None)
    if occurrence is None or transcript != generation.get('spoken_text'):
        raise ValueError('The saved Fish context no longer matches the original dialogue.')
    return transcript, occurrence


def describe(app, data):
    record, source, piece, _, _, _ = saved_context_audition_source(app, data)
    assert_audio_usable(piece, app.store, audition_only=True)
    if context_path(app.store, piece) is None:
        raise ValueError('The saved Fish context is unavailable.')
    transcript, (_, start, stop) = _lexical_context(record, source, piece)
    return {'expected_transcript': transcript[:start] + piece['insert_expected_text'] + transcript[stop:]}


def save(app, data, status=lambda message: None):
    record, source, piece, _, _, _ = saved_context_audition_source(app, data)
    for asset in (source, piece):
        assert_audio_usable(asset, app.store, audition_only=True)
        if media_digest(asset) != asset['sha256']:
            raise ValueError('A source recording changed after the Fish edit.')

    context = context_media(piece)
    path = context_path(app.store, piece)
    if path is None or media_digest({'path': path}) != context['sha256']:
        raise ValueError('The saved Fish context is missing or changed.')
    count = context.get('sample_count')
    media = probe(path)
    if (media['sample_rate'] != RATE or media['channels'] != 1 or
            type(count) is not int or not 0 < count <= RATE * MAX_AUDIO_SECONDS):
        raise ValueError('Saved Fish context needs valid mono 44.1 kHz audio.')
    pcm = decode(path)
    if len(pcm) != count:
        raise ValueError('The saved Fish context does not match the returned clip.')
    begin, end = _generated_region(piece, pcm, decode(piece['path']))

    transcript, (segment, start, stop) = _lexical_context(record, source, piece)
    expected = WORDS.findall(piece['insert_expected_text'])
    before, after = _tokens(transcript[:start])[-3:], _tokens(transcript[stop:])[:3]
    supplied = data.get('transcript')
    if supplied is None:
        evidence = read(app.store, piece)
        pending = None
    else:
        if not isinstance(supplied, str) or len(supplied) > 50_000 or not _tokens(supplied):
            raise ValueError('Enter the words heard in the complete Fish context.')
        if not source.get('words'):
            raise ValueError('Align the original dialogue take before assembling a Fish context.')
        _locate(_tokens(supplied), before, _tokens(piece['insert_expected_text']), after)
        expected_words = [{'word': word} for word in WORDS.findall(supplied)]
        from alignment_backend import validate_words
        status('Estimating word timings for the reviewed transcript…')
        aligned = app._align_transcript(record, path, supplied)
        validate_words(aligned['words'], expected_words, count / RATE)
        if (aligned.get('audio_sha256', context['sha256']) != context['sha256'] or
                media_digest({'path': path}) != context['sha256'] or
                any(media_digest(asset) != asset['sha256'] for asset in (source, piece))):
            raise ValueError('A recording changed during alignment. No timings were saved.')
        pending = {'format': 'dmspeaks-native-join-evidence-1', 'insert_sha256': piece['sha256'],
                   'replacement': piece['insert_expected_text'], 'timing_basis': 'estimated_supplied_transcript',
                   'source_evidence': {'audio_sha256': source['sha256'], 'words': source.get('words', [])},
                   'context_evidence': {'audio_sha256': context['sha256'], 'words': aligned['words']}}
        evidence = pending
    observed = _tokens(evidence.get('recognized_as') or piece['insert_expected_text'])
    if not expected or len(observed) != len(expected):
        raise ValueError('The saved recognition does not identify the replacement words.')

    words = evidence['context_evidence']['words']
    geometry = {'sample_count': count, 'sample_rate': RATE, 'channels': 1}
    labels, intervals = _timed_words({'id': piece['id'], **geometry, 'words': words})
    first, last = _locate(labels, before, observed, after)
    if any(not max(begin, lo) < min(end, hi) for lo, hi in intervals[first:last]):
        raise ValueError('The recognized replacement falls outside the generated audio.')
    # Keep every observed neighbor, including extra speech. The join planner
    # needs those labels to find a verified cut or hold the recording.
    mapped = [{key: word[key] for key in ('word', 'start', 'end')} for word in words]
    order = segment['order']
    for index, text in enumerate(expected, first):
        mapped[index].update(observed_word=mapped[index]['word'], word=text, segment_order=order)
    timing_basis = evidence.get('timing_basis') or 'estimated_join_word_evidence'
    receipt = {'context_sha256': context['sha256'], 'insert_sha256': piece['sha256'],
               'timing_sha256': hashlib.sha256(json.dumps(mapped, sort_keys=True).encode()).hexdigest()}
    item = {**geometry, 'kind': 'recording-source', 'path': str(path), 'sha256': context['sha256'],
            'seconds': count / RATE, 'name': 'Fish Context · ' + piece['value'], 'words': mapped,
            'timing_source': timing_basis, 'native_context_take': receipt,
            'dataset': record['dataset'], 'line_id': record['id'], 'record_id': source['record_id'],
            'source_revision': record['revision'], 'source_metadata_revision': record.get('metadata_revision'),
            'source_take_asset_id': source['id'], 'source_clip_id': piece['id'],
            'generation': {'mode': 'native_context', 'slot_order': order,
                           'spoken_text': ' '.join(word['word'].strip() for word in mapped),
                           'segments': [{**segment, 'value': piece['value']}]}}
    with app.store.lock:
        current = app.record(data)
        if (current['revision'] != record['revision'] or
                current.get('metadata_revision') != record.get('metadata_revision') or
                app.store.asset(source['id']) != source or app.store.asset(piece['id']) != piece):
            raise ValueError('The dialogue or recording changed. Reopen the recording review.')
        rows = app.store.db.execute("SELECT metadata FROM assets WHERE json_valid(metadata) "
                                    "AND json_extract(metadata, '$.source_clip_id')=?", (piece['id'],)).fetchall()
        existing = None
        for row, in rows:
            saved = json.loads(row)
            if saved.get('native_context_take') == receipt and saved.get('timing_source') == timing_basis:
                assert_audio_usable(saved, app.store)
                if media_digest(saved) != receipt['context_sha256']:
                    raise ValueError('The saved context recording changed.')
                existing = saved
                break
        changed = []
        if pending is not None:
            from native_join_evidence import set_evidence
            changed.append(set_evidence(piece, pending))
        if existing is None:
            changed.append(item)
        if changed:
            app.store.add_assets(changed)
        return {'asset_id': (existing or item)['id']}
