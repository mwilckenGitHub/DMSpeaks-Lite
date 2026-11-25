"""Promote an exact, listened-to reconstruction without approving its solo clips.

The editable graph and every current media dependency are bound to the review.
Track and folder organization can retain the review when the mix matches exactly.
"""
import copy
import hashlib
import json
import time


NATIVE_DIALOGUE_MIX = 'dmspeaks-native-dialogue-mix-1'
GRAPH_VERSION = 2


def literal_export_blocker(asset):
    """Keep audible source markers available as controls, outside delivery."""
    if not asset.get('contextual_assembly'):
        return None
    markers = []
    for layer in asset.get('layers', ()):
        slot = layer.get('slot') or {}
        marker = slot.get('placeholder')
        if (layer.get('kind') == 'audio' and slot.get('type') not in (None, 'fixed')
                and not slot.get('speech_excluded') and isinstance(marker, str) and marker
                and slot.get('value') == marker and marker not in markers):
            markers.append(marker)
    if markers:
        return 'Choose a replacement or silence for ' + ', '.join(markers) + ' before preparing export.'
    return None


def graph_digest(store, asset, *, version=GRAPH_VERSION):
    return _graph_digest(asset, store.asset, version)


def _graph_digest(asset, lookup_asset, version):
    from content_review import media_digest
    digests = {}

    def normalize(value):
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key in {'asset_id', 'replacement_origin_asset_id'} and item:
                if item not in digests:
                    dependency = lookup_asset(item)
                    actual = media_digest(dependency)
                    if dependency.get('sha256') != actual:
                        raise ValueError('Reconstruction source media changed. Reassemble and review it.')
                    digests[item] = dict(sha256=actual, sample_count=dependency.get('sample_count'),
                                         sample_rate=dependency.get('sample_rate'), channels=dependency.get('channels'))
                result[key] = digests[item]
            else:
                result[key] = normalize(item)
        return result

    fields = ['layers', 'tracks', 'channels', 'sample_count', 'contextual_assembly']
    context = asset.get('contextual_assembly') or {}
    if isinstance(context, dict) and context.get('format') == NATIVE_DIALOGUE_MIX:
        fields.extend(('record_id', 'source_revision', 'source_metadata_revision'))
    graph = {key: asset.get(key) for key in fields}
    if version == GRAPH_VERSION:
        timing = context.get('timed_proposals') if isinstance(context, dict) else None
        if isinstance(timing, dict):
            # Held alternatives are diagnostic and their session IDs are
            # remapped on project open. They never contribute to this master.
            graph['contextual_assembly'] = {**context, 'timed_proposals': {
                key: value for key, value in timing.items() if key != 'held'}}
        graph['selected_contextual_choices'] = {
            region: {'choice_id': group['selected'], 'choice': group['choices'][group['selected']]}
            for region, group in (asset.get('contextual_choices') or {}).items()
        }
    graph = normalize(graph)
    return hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def upgrade_portable_review(asset, lookup_asset):
    """Upgrade a legacy fingerprint before any portable asset IDs are remapped."""
    from content_review import review_matches_media
    delivery = asset.get('contextual_delivery')
    review = asset.get('content_review')
    if (not isinstance(delivery, dict) or not isinstance(review, dict) or
            delivery.get('format') != 'dmspeaks-contextual-delivery-1' or 'graph_version' in delivery or
            review.get('status') != 'user_verified' or review.get('provenance') != 'user_listening' or
            delivery.get('reviewed_master_sha256') != review.get('media_sha256')):
        return
    try:
        if (review_matches_media(asset, review) and
                delivery.get('graph_sha256') == _graph_digest(asset, lookup_asset, 1)):
            asset['contextual_delivery'] = {
                **delivery, 'graph_version': GRAPH_VERSION,
                'graph_sha256': _graph_digest(asset, lookup_asset, GRAPH_VERSION),
            }
    except (KeyError, OSError, ValueError):
        # The project can still open for review. Keep an invalid binding intact.
        return


def assert_reviewed(store, asset):
    from content_review import review_matches_media
    if reason := literal_export_blocker(asset):
        raise ValueError(reason)
    review = asset.get('content_review') or {}
    delivery = asset.get('contextual_delivery') or {}
    version = delivery.get('graph_version', 1)
    if not (review.get('status') == 'user_verified' and
            review.get('provenance') == 'user_listening' and
            review_matches_media(asset, review) and
            delivery.get('format') == 'dmspeaks-contextual-delivery-1' and
            delivery.get('reviewed_master_sha256') == review.get('media_sha256') and
            type(version) is int and version in (1, GRAPH_VERSION) and
            delivery.get('graph_sha256') == graph_digest(store, asset, version=version)):
        raise ValueError('This reconstruction needs a current full-phrase listening review and delivery preparation.')


def retain_mix_review(app, base, candidate):
    """Bind a track or folder move to the same reviewed master if its audio matches."""
    from audio import decode, probe
    from content_review import assert_audio_usable, media_digest, review_matches_media
    from contextual_choices import current_record_for
    from layered_audio import RATE

    review = base.get('content_review') or {}
    if (review.get('status') != 'user_verified' or review.get('provenance') != 'user_listening' or
            any(base.get(key) != candidate.get(key) for key in ('channels', 'sample_count'))):
        return False
    latest = app.store.asset(base['id'])
    if latest != base:
        return False
    try:
        current_record_for(app, latest)
        if not review_matches_media(latest, review):
            return False
        assert_audio_usable(latest, app.store, audition_only=latest.get('audition_only') is True)
        if media_digest(latest) != candidate['sha256']:
            # Portable projects may have a WAV master with the same samples.
            channels = candidate['channels']
            source = probe(latest['path'])
            if (source['sample_rate'] != RATE or source['channels'] != channels or
                    latest.get('sample_rate', RATE) != RATE):
                return False
            if decode(latest['path'], channels=channels) != decode(candidate['path'], channels=channels):
                return False
        delivery = latest.get('contextual_delivery')
        if delivery:
            delivery = {**delivery, 'graph_version': GRAPH_VERSION,
                        'graph_sha256': graph_digest(app.store, candidate)}
    except (ValueError, FileNotFoundError):
        return False

    for key in ('path', 'sha256', 'kind', 'audition_only', 'listening_review', 'content_review',
                'content_review_revision', 'content_review_history'):
        if key in latest:
            candidate[key] = copy.deepcopy(latest[key])
    if delivery:
        candidate['contextual_delivery'] = delivery
    return True


def prepare(app, data, status):
    from audio import decode
    from content_review import assert_audio_usable, media_digest, review_matches_media
    from layered_audio import render_layers
    record = app.record(data)
    asset = app.store.asset(data['asset_id'])
    if (not asset.get('contextual_assembly') or not asset.get('layers') or
            asset.get('replacement_playback') or
            asset.get('record_id') != record['dataset'] + ':' + str(record['index'])):
        raise ValueError('Choose this dialogue’s selected contextual reconstruction.')
    if (asset.get('source_revision') != record['revision'] or
            asset.get('source_metadata_revision') != record.get('metadata_revision')):
        raise ValueError('The dialogue or speaker metadata changed. Reassemble and review it.')
    if reason := literal_export_blocker(asset):
        raise ValueError(reason)
    review = asset.get('content_review') or {}
    if not (review.get('status') == 'user_verified' and review.get('provenance') == 'user_listening'
            and review_matches_media(asset, review)):
        raise ValueError('Listen to and accept this full reconstruction before preparing delivery.')
    assert_audio_usable(asset, app.store, audition_only=True)
    if asset.get('join_proposal'):
        # The full-mix decision and the standalone clip review are separate.
        assert_audio_usable({**asset, 'audition_only': False, 'contextual_assembly': None},
                            app.store)
    digest = graph_digest(app.store, asset)
    status('Checking the reviewed reconstruction against its current layers…')
    pcm, rendered = render_layers(asset['layers'], asset['tracks'], app.store, audition_only=True)
    cached = decode(asset['path'], channels=rendered['channels'])
    if (len(pcm) != len(cached) or not pcm or
            any(abs(a - b) > 2**-23 for a, b in zip(pcm, cached))):
        raise ValueError('The reconstruction differs from its listened-to recording. Rebuild and review it.')
    with app.store.lock:
        current = app.record(data)
        latest = app.store.asset(asset['id'])
        if (current['revision'] != record['revision'] or
                current.get('metadata_revision') != record.get('metadata_revision') or
                latest.get('content_review_revision') != asset.get('content_review_revision') or
                latest.get('content_review') != review or graph_digest(app.store, latest) != digest or
                not review_matches_media(latest, review)):
            raise ValueError('The reconstruction changed during delivery preparation. Review the current version.')
        assert_audio_usable(latest, app.store, audition_only=True)
        if latest.get('join_proposal'):
            assert_audio_usable({**latest, 'audition_only': False, 'contextual_assembly': None},
                                app.store)
        result = copy.deepcopy(latest)
        result.pop('id', None)
        result.update(kind='assembly', audition_only=False, parent_assembly_id=asset['id'],
                      listening_review='full_reconstruction_user_verified',
                      contextual_delivery=dict(format='dmspeaks-contextual-delivery-1',
                          graph_version=GRAPH_VERSION, graph_sha256=digest,
                          reviewed_master_sha256=media_digest(latest), prepared_at=time.time(),
                          review_scope='Selected full reconstruction only; solo clip isolation is not approved.'))
        return app.store.add_asset(result)
