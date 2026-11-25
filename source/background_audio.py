"""Owned background sources and non-destructive arrangement edits; never invoke TTS."""
import copy
import hashlib
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

from audio import decode, encode_preview_pcm, probe
from background_graph import add_layer, remove_layer
from engine_limits import MAX_SEGMENTS
from layered_audio import RATE, render_layers

ROLES = {'background_music', 'background_ambience'}


def current_base(app, asset_id):
    base = app.store.asset(asset_id)
    if not base.get('layers'):
        raise ValueError('Assemble a passage into audio layers first.')
    from processing_definition import require_ordinary_rebuild
    scoped = base.get('kind') == 'scoped-arrangement-preview' and base.get('audition_only') is True
    if not scoped:
        require_ordinary_rebuild(base, allow_track_hierarchy=True)
    elif base.get('replacement_playback'):
        raise ValueError('Return to the selected replacement before editing music or ambience.')
    from content_review import assert_audio_usable
    assert_audio_usable(base, app.store, audition_only=scoped)
    dataset, index = base.get('record_id', '').rsplit(':', 1)
    from clip_ownership import current_record
    record = current_record(app.store, dataset, int(index))
    if base.get('source_revision') != record['revision'] or base.get('source_metadata_revision') != record.get('metadata_revision'):
        raise ValueError('The source changed. Reassemble before editing background tracks.')
    return base


def import_source(app, content, data, name):
    base = current_base(app, data['asset_id'])
    if not content or len(content) > 80_000_000:
        raise ValueError('Choose a background audio file of at most 80 MB.')
    name = Path(name).name
    suffix = Path(name).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,10}', suffix):
        suffix = '.audio'
    ident = uuid.uuid4().hex
    path = app.audio.root / (ident + suffix)
    path.write_bytes(content)
    try:
        info = probe(path)
        if info['channels'] not in (1, 2):
            raise ValueError('Choose mono or stereo background audio.')
        if base.get('kind') == 'scoped-arrangement-preview':
            from processing_arrangement_preview import MAX_AUDITION_SECONDS, MAX_SOURCE_BYTES
            if len(content) > MAX_SOURCE_BYTES:
                raise ValueError('Audio Lab supports background source files up to 64 MB.')
            if info['seconds'] > MAX_AUDITION_SECONDS:
                raise ValueError(f'Audio Lab supports background source files up to {MAX_AUDITION_SECONDS} seconds.')
        count = len(decode(path, channels=info['channels'])) // info['channels']
        if not 0 < count <= RATE * 1800:
            raise ValueError('Background audio must be between zero and 30 minutes.')
        return app.store.add_asset(dict(id=ident, kind='background-source', name=name,
            dataset=base['dataset'], line_id=base['line_id'], record_id=base['record_id'],
            source_revision=base['source_revision'], path=str(path), sha256=hashlib.sha256(content).hexdigest(),
            sample_count=count, sample_rate=RATE, channels=info['channels'], seconds=count / RATE,
            source_audio_metadata=info, source_bytes_preserved=True, words=[], timing_source='not_speech'))
    except Exception:
        path.unlink(missing_ok=True)
        raise


def save_mix(app, base, layers, tracks, status, *, pending_source=None, validate_commit=None):
    if base.get('replacement_playback'):
        from replacement_playback import independent_layers,rebuild
        original,merged=independent_layers(app.store,base,layers)
        revised=save_mix(app,original,merged,tracks,status)
        return rebuild(app,base,revised,status)
    from processing_group_lock import validate_transition, validate_scheduled
    validate_transition(base['tracks'], base['layers'], tracks, layers)
    store = app.store
    if pending_source is not None:
        def get_asset(ident):
            return pending_source if ident == pending_source['id'] else app.store.asset(ident)
        store = SimpleNamespace(asset=get_asset)
    scoped = base.get('kind') == 'scoped-arrangement-preview' and base.get('audition_only') is True
    if scoped:
        stack, hierarchy = scoped_background_layout(base, tracks, layers)
        from processing_arrangement_preview import MAX_AUDITION_LAYERS, MAX_AUDITION_SECONDS, check_source_budget
        if len(layers)>MAX_AUDITION_LAYERS:
            raise ValueError(f'Audio Lab supports up to {MAX_AUDITION_LAYERS} clips.')
        if any(layer.get('anchor_start_sample',0)>MAX_AUDITION_SECONDS*RATE for layer in layers):
            raise ValueError(f'Place the background inside the {MAX_AUDITION_SECONDS}-second Audio Lab timeline.')
        check_source_budget(layers, store)
    elif base.get('track_hierarchy') is not None:
        from audio_track_hierarchy import TrackHierarchy, update_track_references
        hierarchy = update_track_references(base['track_hierarchy'], base['tracks'], tracks)
        model = TrackHierarchy.from_mapping(hierarchy, tracks, tracks=tracks, layers=layers)
        locks = model.effective_locks()
        if any(track.get('group_locked', False) != locks[track['id']] for track in tracks):
            raise ValueError('The saved track locks do not match their folders.')
    status('Mixing separate speech and background tracks; no speech generation…')
    pcm, result = render_layers(layers, tracks, store, audition_only=scoped)
    validate_scheduled(base['tracks'], base['layers'], result['layers'])
    if pending_source is not None:
        from recording_import import finish_source
        finish_source(pending_source, result['layers'])
    path = app.audio.root / (uuid.uuid4().hex + '.flac')
    try:
        encode_preview_pcm(pcm, path, channels=result['channels'])
        excluded = {'id', 'path', 'slots', 'words', 'layers', 'tracks', 'sample_count', 'seconds',
                    'sha256', 'content_review', 'content_review_revision', 'content_review_history'}
        if scoped:
            excluded.update({'processing_definition','processing_preview','dry_sample_count','track_hierarchy'})
        asset = {k: copy.deepcopy(v) for k, v in base.items() if k not in excluded}
        asset.update(result, path=str(path), parent_assembly_id=base['id'],
                     sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        if scoped:
            return app._save_scoped_arrangement(asset,stack,hierarchy,status,
                                                pending_source=pending_source,validate_commit=validate_commit)
        if base.get('track_hierarchy') is not None:
            asset['track_hierarchy'] = hierarchy
        if validate_commit is not None:
            with app.store.lock:
                validate_commit()
                app.store.add_assets([asset] if pending_source is None else [pending_source, asset])
            return asset
        return app.store.add_asset(asset)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def scoped_background_layout(base, tracks, layers):
    """Add the new background below the stack or remove its existing track."""
    from audio_track_hierarchy import TrackHierarchy
    from processing_arrangement_preview import MAX_AUDITION_TRACKS
    if len(tracks)>MAX_AUDITION_TRACKS:
        raise ValueError(f'Audio Lab supports up to {MAX_AUDITION_TRACKS} audio tracks.')
    previous = {track['id'] for track in base['tracks']}
    current = {track['id'] for track in tracks}
    stack = [item for item in base['processing_definition']
             if 'processing' in item or item['id'] in current]
    added = [track['id'] for track in tracks if track['id'] not in previous]
    stack.extend({'id':ident} for ident in added)
    hierarchy = copy.deepcopy(base.get('track_hierarchy'))
    if hierarchy is not None:
        for items in [hierarchy['roots'],*(group['children'] for group in hierarchy['groups'])]:
            items[:] = [item for item in items if item['type']!='track' or item['id'] not in previous-current]
        hierarchy['roots'].extend({'type':'track','id':ident} for ident in added)
        model = TrackHierarchy.from_mapping(hierarchy,stack,tracks=tracks,layers=layers)
        locks = model.effective_locks()
        if any(track.get('group_locked',False)!=locks[track['id']] for track in tracks):
            raise ValueError('The saved track locks do not match their folders.')
    return stack,hierarchy


def add(app, data, status):
    base = current_base(app, data['asset_id'])
    if len(base['layers']) >= MAX_SEGMENTS:
        raise ValueError('This arrangement is at the current runtime layer limit.')
    source = app.store.asset(data['source_asset_id'])
    if source.get('kind') != 'background-source' or source.get('record_id') != base['record_id']:
        raise ValueError('Choose a background source imported for this dialogue.')
    layers, tracks = add_layer(base, source, data.get('role', 'music'),
        data.get('name') or source['name'], 'background-' + uuid.uuid4().hex,
        data.get('anchor_start_sample', 0), data.get('gain_db', -18))
    from background_loop import validate_loop
    from background_duck import validate_ducking
    policy=validate_loop(data.get('background_loop'),source['sample_count'])
    if policy:
        layers[-1]['background_loop']=policy
        if policy['enabled']:layers[-1]['source_out_sample']=policy['span_samples']
    if data.get('ducking') is not None:tracks[-1]['ducking']=validate_ducking(data['ducking'])
    return save_mix(app, base, layers, tracks, status)


def remove(app, data, status):
    base = current_base(app, data['asset_id'])
    layers, tracks = remove_layer(base, data['layer_id'])
    return save_mix(app, base, layers, tracks, status)


def update(app,data,status):
    from background_loop import validate_loop
    from background_duck import validate_ducking
    base=current_base(app,data['asset_id']);layers=copy.deepcopy(base['layers']);tracks=copy.deepcopy(base['tracks'])
    layer=next((l for l in layers if l['id']==data.get('layer_id') and l.get('clip_role') in ROLES),None)
    if layer is None:raise ValueError('Choose a background clip.')
    source=app.store.asset(layer['asset_id']);track=next(t for t in tracks if t['id']==layer['track_id'])
    if 'background_loop' in data:
        policy=validate_loop(data['background_loop'],source['sample_count'])
        previous=layer.get('background_loop') or {}
        old_span=previous['span_samples'] if previous.get('enabled') else source['sample_count']
        span=policy['span_samples'] if policy and policy['enabled'] else source['sample_count']
        if layer.get('source_in_sample',0)>=span:raise ValueError('The loop span ends before this trimmed section.')
        if layer.get('source_out_sample',old_span)==old_span:layer['source_out_sample']=span
        elif layer.get('source_out_sample',0)>span:raise ValueError('The loop span is shorter than this trimmed section.')
        if policy:layer['background_loop']=policy
        else:layer.pop('background_loop',None)
    if 'ducking' in data:track['ducking']=validate_ducking(data['ducking'])
    return save_mix(app,base,layers,tracks,status)
