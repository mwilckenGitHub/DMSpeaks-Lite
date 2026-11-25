"""Exclude explicitly rejected recordings without inventing verification claims."""
import hashlib
import json
import time
from pathlib import Path


def media_digest(asset):
    digest = hashlib.sha256()
    with Path(asset['path']).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _contextual_dialogue(store, asset):
    """Resolve the replacement clips in the matching authored source for review."""
    from contextual_delivery import NATIVE_DIALOGUE_MIX

    assembly = asset.get('contextual_assembly') or {}
    owner = asset.get('record_id')
    assembly_format = assembly.get('format')
    if assembly_format not in {'dmspeaks-context-packets-1', NATIVE_DIALOGUE_MIX} or not isinstance(owner, str):
        return None
    from clip_ownership import current_record

    try:
        dataset, index = owner.rsplit(':', 1)
        record = current_record(store, dataset, int(index))
    except (KeyError, ValueError, IndexError):
        return None
    if (asset.get('source_revision') != record['revision'] or
            asset.get('source_metadata_revision') != record.get('metadata_revision')):
        return None

    source = record['text']
    layers = {layer['id']: layer for layer in asset.get('layers', ())}
    if assembly_format == NATIVE_DIALOGUE_MIX:
        native = asset.get('native_context') or {}
        replacement = layers.get('proposed-replacement', {})
        if (replacement.get('kind') != 'audio' or
                replacement.get('slot', {}).get('placeholder') != native.get('placeholder')):
            return None
        occurrences = [(native.get('slot_order'), replacement)]
    else:
        occurrences = []
        for packet in assembly.get('packets', ()):
            region = packet['region_id']
            layer = layers.get(f'packet:{len(region)}:{region}:word', {})
            occurrences.append((packet.get('slot_order'), layer))
    replacements = []
    legacy_segments = None
    for order, layer in occurrences:
        slot = layer.get('slot', {})
        start, marker, value = (slot.get(field) for field in ('source_start', 'placeholder', 'value'))
        if 'source_start' not in slot:
            # Older bundles recorded occurrence order and type but no offset.
            # Parse the exact authored revision and require all three to agree.
            if legacy_segments is None:
                from performance_plan import parse_record
                try:
                    legacy_segments = {s['order']: s for s in parse_record(record)['segments']}
                except ValueError:
                    return None
            if type(order) is not int or order != layer.get('slot_order') or order != slot.get('order'):
                return None
            segment = legacy_segments.get(order)
            if (segment is None or segment['type'] == 'fixed' or segment.get('speech_excluded')
                    or (segment['type'], segment['placeholder']) != (slot.get('type'), marker)):
                return None
            start = segment['source_start']
        if (type(start) is not int or start < 0 or not isinstance(marker, str) or not marker
                or not isinstance(value, str) or not source.startswith(marker, start)):
            return None
        replacements.append((start, start + len(marker), value))

    pieces, cursor = [], 0
    for start, end, value in sorted(replacements):
        if start < cursor:
            return None
        pieces.extend((source[cursor:start], value))
        cursor = end
    return ''.join([*pieces, source[cursor:]]) if replacements else None


def review_info(store, asset_id):
    from contextual_delivery import literal_export_blocker
    from native_join_evidence import available, context_path, saved_context_available

    asset = store.asset(asset_id)
    review = asset.get('content_review') or {}
    with store.lock:
        rows = store.db.execute("select metadata from assets where json_extract(metadata,'$.source_take_asset_id')=? order by rowid desc limit 33", (asset_id,)).fetchall()
    children = [json.loads(row[0]) for row in rows]
    trace = asset.get('generation') or {}
    trimmed = next((layer for layer in asset.get('layers', ())
                    if asset.get('join_proposal') and layer.get('id') == 'proposed-replacement'
                    and layer.get('kind') == 'audio'), None)
    clip = store.asset(trimmed['asset_id']) if trimmed else {}
    return {'asset_id': asset_id, 'media_sha256': media_digest(asset),
            'review': review, 'review_revision': asset.get('content_review_revision', 0),
            'requested_text': trace.get('spoken_text') or asset.get('insert_expected_text') or asset.get('value') or '',
            'contextual_dialogue': _contextual_dialogue(store, asset),
            'timing_source': asset.get('timing_source', 'unavailable'),
            'source_take_asset_id': asset.get('source_take_asset_id'),
            'has_native_context_window': bool(asset.get('native_context_window')),
            'suggested_join_available': available(store, asset),
            'native_context_available': context_path(store, asset) is not None,
            'saved_context_available': saved_context_available(store, asset),
            'contextual_reconstruction': bool(asset.get('contextual_assembly')),
            'contextual_export_blocker': literal_export_blocker(asset),
            'trimmed_join_clip': {'id': clip['id'], 'accepted': is_delivery_eligible(clip)}
                                 if clip.get('native_join_slice') else None,
            'contextual_delivery_prepared': bool(asset.get('contextual_delivery')) and not asset.get('audition_only'),
            'isolation_review_required': requires_isolation_review(asset),
            'isolation_usable': isolation_usable(asset),
            'children': [{'id': child['id'], 'slot_order': child.get('slot_order'),
                          'value': child.get('value'), 'review': child.get('content_review') or {}}
                         for child in children[:32]], 'more_children': len(children) > 32}


def list_reviews(store, record, before=None, *, target=None, performance=None, clips_only=False, clip_value=None):
    from recording_import import original_recording_name

    if before is not None and (type(before) is not int or before < 1):
        raise ValueError('Invalid review page.')
    if clips_only and target is None:
        raise ValueError('Choose a delimiter occurrence to list its clips.')
    if clip_value is not None and (not clips_only or not isinstance(clip_value, str) or not clip_value):
        raise ValueError('Choose a replacement value to list its prior clips.')
    record_id = record['dataset'] + ':' + str(record['index'])
    scope = ''
    parameters = [record_id, not record.get('source_id_ambiguous'), record['dataset'], record['id']]
    if target is not None:
        from performance_plan import parse_record
        if not isinstance(target, dict) or set(target) != {'order', 'type', 'placeholder'} or type(target['order']) is not int:
            raise ValueError('Choose one delimiter occurrence.')
        performance = performance or {}
        segments = parse_record(record, performance.get('player', {}), performance)['segments']
        segment = next((item for item in segments if item['order'] == target['order']), None)
        if (not segment or segment['type'] == 'fixed' or segment.get('speech_excluded') or
                segment['type'] != target['type'] or segment['placeholder'] != target['placeholder']):
            raise ValueError('The selected delimiter changed. Choose it again.')
        scope = """and json_extract(metadata,'$.source_revision') is ?
            and json_extract(metadata,'$.source_metadata_revision') is ?
            and ((json_extract(metadata,'$.kind')='piece'
                  and json_extract(metadata,'$.slot_order')=?
                  and json_extract(metadata,'$.slot_type')=?)
                 or (json_extract(metadata,'$.kind')='inspection-preview'
                     and json_extract(metadata,'$.native_context.slot_order')=?
                     and json_extract(metadata,'$.native_context.placeholder')=?)
                 or (json_extract(metadata,'$.contextual_assembly.format')='dmspeaks-context-packets-1'
                     and exists (select 1 from json_each(metadata,'$.layers') as layer
                         where json_extract(layer.value,'$.slot_order')=?
                           and json_extract(layer.value,'$.slot.type')=?
                           and json_extract(layer.value,'$.slot.placeholder')=?)))"""
        parameters.extend([record['revision'], record.get('metadata_revision'),
                           target['order'], target['type'], target['order'], target['placeholder'],
                           target['order'], target['type'], target['placeholder']])
    if clip_value is not None:
        scope += """and (case when json_extract(metadata,'$.kind')='piece'
                    then json_extract(metadata,'$.value')
                    else (select json_extract(layer.value,'$.slot.value') from json_each(metadata,'$.layers') as layer
                          where json_extract(layer.value,'$.slot_order')=?
                            and json_extract(layer.value,'$.slot.type')=?
                            and json_extract(layer.value,'$.slot.placeholder')=? limit 1)
                    end)=?"""
        parameters.extend([target['order'], target['type'], target['placeholder'], clip_value])
    parameters.extend([before, before])
    with store.lock:
        rows = store.db.execute(f"""select rowid,metadata from assets where
            (json_extract(metadata,'$.record_id')=? or
             (? and json_extract(metadata,'$.record_id') is null
                and json_extract(metadata,'$.dataset')=? and json_extract(metadata,'$.line_id')=?))
            and (json_extract(metadata,'$.kind') in ('take','piece','assembly','inspection-preview')
                 or (json_extract(metadata,'$.kind')='recording-source'
                     and json_extract(metadata,'$.record_id') is not null
                     and json_extract(metadata,'$.native_join_slice') is null))
            {scope}
            and (? is null or rowid<?) order by rowid desc limit 51""", parameters).fetchall()
    items = []
    journals = None
    for _, raw in rows[:50]:
        asset = json.loads(raw)
        item = {key: asset.get(key) for key in
                ['id', 'kind', 'slot_order', 'value', 'seconds', 'content_review',
                 'native_context', 'source_revision', 'source_metadata_revision']}
        if asset['kind'] == 'recording-source':
            item['name'] = asset.get('name')
        elif asset['kind'] == 'take' and asset.get('imported_recording_name'):
            if journals is None:
                journals = {}
                for take in store.takes(record_id):
                    journals.setdefault(take['asset_id'], []).append(take)
            name = original_recording_name(asset, journals.get(asset['id'], ()))
            if name:
                item.update(original_import=True, name=name)
        if clips_only:
            if asset.get('kind') == 'piece':
                item.update(media_id=asset['id'], sample_bounds=None)
            else:
                layers = [layer for layer in asset.get('layers', []) if
                          layer.get('slot_order') == target['order'] and
                          (layer.get('slot') or {}).get('type') == target['type'] and
                          (layer.get('slot') or {}).get('placeholder') == target['placeholder']]
                if len(layers) != 1:
                    continue
                layer = layers[0]
                start, end = layer.get('source_in_sample'), layer.get('source_out_sample')
                if (layer.get('kind') != 'audio' or not layer.get('asset_id') or
                        type(start) is not int or type(end) is not int or not 0 <= start < end):
                    continue
                item.update(media_id=layer['asset_id'], sample_bounds=[start, end],
                            value=layer['slot'].get('value'), seconds=(end-start)/44100)
            items.append(item)
            continue
        if target and (asset.get('contextual_assembly') or {}).get('format') == 'dmspeaks-context-packets-1':
            layer = next((layer for layer in asset.get('layers', [])
                          if layer.get('slot_order') == target['order'] and
                          (layer.get('slot') or {}).get('type') == target['type'] and
                          (layer.get('slot') or {}).get('placeholder') == target['placeholder']), None)
            if layer:
                item.update(contextual_mix=True, slot_order=target['order'], value=layer['slot'].get('value'))
        items.append(item)
    return {'items': items, 'next_before': rows[49][0] if len(rows) > 50 else None}


def check_context_tracks(app, data):
    """Open only saved inspection tracks belonging to the current dialogue."""
    record = app.record(data)
    asset = app.store.asset(data['asset_id'])
    if (asset.get('kind') != 'inspection-preview' or not asset.get('audition_only') or
            not isinstance(asset.get('native_context'), dict) or not asset.get('layers') or
            asset.get('record_id') != f"{record['dataset']}:{record['index']}" or
            asset.get('source_revision') != record['revision'] or
            asset.get('source_metadata_revision') != record.get('metadata_revision')):
        raise ValueError('Choose saved context tracks for the current dialogue revision.')
    assert_audio_usable(asset, app.store, audition_only=True)
    for layer in asset['layers']:
        if layer.get('kind') == 'audio':
            source = app.store.asset(layer['asset_id'])
            if requires_isolation_review(source) and not source.get('native_join_slice'):
                assert_audio_usable(source, app.store)
    return {'id': asset['id']}


def save_review(store, data):
    with store.lock:
        asset = store.asset(data['asset_id'])
        result = apply_review(asset, data, lambda: media_digest(asset))
        store.add_asset(asset)
    return result


def apply_review(asset, data, read_digest):
    """Record a decision for one asset, leaving persistence to the caller."""
    status = data.get('status')
    if status not in {'user_verified', 'rejected', 'unreviewed'}:
        raise ValueError('Choose accepted, rejected, or unreviewed.')
    if status != 'unreviewed' and data.get('listened') is not True:
        raise ValueError('Listen to the recording before accepting or rejecting its content.')
    note = data.get('note', '')
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError('Use a review note of at most 2000 characters.')
    revision = data.get('expected_revision')
    if type(revision) is not int or revision < 0:
        raise ValueError('Refresh this recording review before saving.')
    if revision != asset.get('content_review_revision', 0):
        raise ValueError('This review changed. Reopen it before saving.')
    digest = read_digest()
    if data.get('media_sha256') != digest:
        raise ValueError('The audio changed. Listen to the current recording before reviewing it.')
    if status == 'user_verified' and requires_isolation_review(asset):
        if data.get('isolated_content') is not True or data.get('complete_pronunciation') is not True:
            raise ValueError('Confirm that this clip contains only its intended speech, with no neighboring sounds and no clipped pronunciation.')
    previous = asset.get('content_review')
    if previous:
        asset['content_review_history'] = [*asset.get('content_review_history', []), previous][-20:]
    asset['content_review'] = {'status': status, 'provenance': 'user_listening' if status != 'unreviewed' else 'user_reset',
                               'note': note, 'media_sha256': digest, 'reviewed_at': time.time()}
    if status == 'user_verified' and requires_isolation_review(asset):
        asset['content_review'].update(isolated_content=True, complete_pronunciation=True)
    asset['content_review_revision'] = revision + 1
    return {'asset_id': asset['id'], 'review': asset['content_review'], 'review_revision': revision + 1}


def is_rejected(asset):
    review = asset.get('content_review')
    return isinstance(review, dict) and review.get('status') == 'rejected'


def requires_isolation_review(asset):
    """Native/extracted insert audio is quarantined, including legacy native pieces."""
    return asset.get('isolation_review_required') is True or (
        asset.get('kind') == 'piece' and isinstance(asset.get('native_edit_provenance'), dict))


def review_matches_media(asset, review):
    """Bind a listening decision to its master or a recorded delivery conversion.

    An export may encode a reviewed master or apply authored effects. Keep the
    original listening decision intact; the converted bytes were not separately
    listened to. Both hashes and the processing source must agree.
    """
    actual = media_digest(asset)
    reviewed = review.get('media_sha256')
    if reviewed == actual:
        return True
    derivation = asset.get('delivery_derivation') or {}
    processing = asset.get('clip_processing') or {}
    return bool(reviewed and isinstance(derivation, dict) and isinstance(processing, dict) and
                derivation.get('format') == 'dmspeaks-reviewed-delivery.1' and
                derivation.get('reviewed_master_sha256') == reviewed and
                processing.get('source_sha256') == derivation.get('source_sha256') and
                bool(derivation.get('source_sha256')) and
                derivation.get('audio_sha256') == actual)


def isolation_usable(asset):
    if not requires_isolation_review(asset):
        return True
    review = asset.get('content_review') or {}
    if not (review.get('status') == 'user_verified' and
            review.get('provenance') == 'user_listening' and
            review.get('isolated_content') is True and
            review.get('complete_pronunciation') is True):
        return False
    # ASR/forced alignment never approves isolation. Arbitrary changed bytes
    # cannot inherit a decision; recorded delivery conversions retain provenance.
    try:
        return review_matches_media(asset, review)
    except (OSError, KeyError):
        return False


def is_delivery_eligible(asset):
    return not asset.get('audition_only') and not is_rejected(asset) and isolation_usable(asset)


def assert_narration_usable(asset):
    """A narrator choice requires an actual, byte-bound listening decision."""
    review = asset.get('content_review') or {}
    if not (review.get('status') == 'user_verified' and
            review.get('provenance') == 'user_listening' and
            review_matches_media(asset, review)):
        raise ValueError('This narrator recording needs a current listening review before mixing or export.')


def assert_audio_usable(asset, store, *, audition_only=False):
    """Check present audio dependencies, never historical provenance links.

    Ordinary unreviewed audio remains usable, not verified. Native insert clips
    must pass a separate, byte-bound isolation review. A repaired child does not
    inherit rejection from its source take or an earlier arrangement.
    """
    checked, visiting = set(), set()

    def visit(current):
        ident = current.get('id') or ('object', id(current))
        if is_rejected(current):
            raise ValueError('This recording was rejected in content review. '
                             'Choose or render a replacement before using its audio.')
        if current.get('audition_only') and not audition_only:
            raise ValueError('Inspection audio is for audition only. Review and assemble delivery clips separately.')
        if current.get('contextual_assembly') and not audition_only:
            from contextual_delivery import assert_reviewed
            assert_reviewed(store, current)
        if not audition_only and not isolation_usable(current):
            raise ValueError('This insert is awaiting isolation review, or its audio changed. Listen to the individual clip and verify its complete intended speech before assembly or export.')
        if ident in visiting:
            raise ValueError('Audio dependencies contain a cycle.')
        if ident in checked:
            return
        visiting.add(ident)
        for layer in current.get('layers') or current.get('slots') or []:
            if layer.get('kind', 'audio') in {'direction', 'silence'}:
                continue
            dependency = layer.get('asset_id')
            if dependency:
                child = store.asset(dependency)
                if layer.get('clip_role') == 'visual_narration':
                    assert_narration_usable(child)
                visit(child)
        visiting.remove(ident)
        checked.add(ident)

    visit(asset)
