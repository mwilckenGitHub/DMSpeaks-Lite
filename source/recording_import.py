"""Place intact new and saved recordings on a dialogue timeline."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from audio import encode_preview_pcm, probe
from engine_limits import MAX_AUDIO_SECONDS, MAX_SEGMENTS
from layered_audio import RATE, render_layers
from vorbis_pcm import single_stream_end


def import_recording(app, content, data, filename):
    record = app.record(data)
    check_revisions(record, data)
    base, start = recording_destination(app, data, record)
    name = Path(filename.replace('\\', '/')).name
    suffix = Path(name).suffix.lower()
    if suffix not in {'.wav', '.ogg', '.flac', '.mp3'} or len(name) > 240 or not name.isprintable():
        raise ValueError('Choose a WAV, Ogg, FLAC, or MP3 recording with a valid filename.')

    identity = recording_identity(record)
    source_id = uuid4().hex
    source_path = app.audio.root / (source_id + suffix)
    try:
        source_path.write_bytes(content)
        info = probe(source_path, expected_format=suffix[1:])
        if info['channels'] not in (1, 2) or not 1 <= info['sample_rate'] <= 192_000:
            raise ValueError('Choose mono or stereo audio at up to 192 kHz.')
        if suffix == '.ogg':
            single_stream_end(source_path)
        source = {
            **identity, 'id': source_id, 'kind': 'recording-source', 'name': name,
            'path': str(source_path), 'sha256': hashlib.sha256(content).hexdigest(),
            'source_bytes_preserved': True, 'source_audio_metadata': info,
            'sample_rate': RATE, 'channels': info['channels'], 'words': [],
            'timing_source': 'unavailable',
        }
        return place_recording(app, data, identity, source, base, start, name=name, pending=True)
    except Exception:
        source_path.unlink(missing_ok=True)
        raise


def reuse_recording(app, data):
    """Place an existing original without uploading or rewriting its media."""
    record = app.record(data)
    check_revisions(record, data)
    identity = recording_identity(record)
    source = app.store.asset(data['asset_id'])
    if source.get('record_id') != identity['record_id']:
        raise ValueError('Choose an original import from this dialogue.')
    journals = ([item for item in app.store.takes(identity['record_id']) if item['asset_id'] == source['id']]
                if source.get('kind') == 'take' else [])
    name = original_recording_name(source, journals)
    if not name:
        raise ValueError('Choose an original import from this dialogue.')
    from content_review import assert_audio_usable
    assert_audio_usable(source, app.store)
    base, start = recording_destination(app, data, record)
    return place_recording(app, data, identity, source, base, start, name=name, pending=False)


def original_recording_name(asset, journals=()):
    """Recognize intact browser imports and journaled desktop attachments."""
    if asset.get('kind') == 'recording-source':
        name = asset.get('name')
        return name if isinstance(name, str) and name else 'Imported Recording'
    name = asset.get('imported_recording_name')
    if asset.get('kind') != 'take' or asset.get('generation') or not isinstance(name, str) or not name:
        return None
    if len(journals) != 1:
        return None
    journal = journals[0]
    provenance = journal.get('provenance')
    if (journal.get('asset_id') == asset.get('id') and journal.get('record_id') == asset.get('record_id') and
            journal.get('generation_mode') == 'imported' and isinstance(provenance, dict) and
            provenance.get('method') == 'manual_import' and provenance.get('name') == name):
        return name
    return None


def recording_destination(app, data, record):
    base = None
    if 'base_asset_id' in data:
        from background_audio import current_base
        base = current_base(app, data['base_asset_id'])
        if base['record_id'] != f"{record['dataset']}:{record['index']}" or base.get('replacement_playback'):
            raise ValueError('Choose the dialogue’s main arrangement before adding a recording track.')
        if len(base['layers']) >= MAX_SEGMENTS:
            raise ValueError('This arrangement is at its clip limit.')
    start = data.get('start_sample', 0)
    if type(start) is not int or not 0 <= start < RATE * MAX_AUDIO_SECONDS:
        raise ValueError('Choose a playhead position within the timeline.')
    return base, start


def recording_identity(record):
    return {
        'dataset': record['dataset'], 'line_id': record['id'],
        'record_id': f"{record['dataset']}:{record['index']}",
        'source_revision': record['revision'],
        'source_metadata_revision': record.get('metadata_revision'),
    }


def place_recording(app, data, identity, source, base, start, *, name, pending):
    track_id = source['id'] if pending else uuid4().hex
    track = {'id': 'recording-' + track_id, 'name': name if base else 'Recording'}
    layer = {
        'id': uuid4().hex, 'track_id': track['id'], 'asset_id': source['id'],
        'kind': 'audio', 'clip_role': 'imported_audio', 'slot_order': 0,
        'anchor_start_sample': start, 'source_in_sample': 0,
        'label': name, 'slot': {'type': 'imported_audio', 'spoken_text': name},
    }
    if not pending and source.get('kind') == 'recording-source':
        layer['source_out_sample'] = source['sample_count']

    def validate_commit():
        check_revisions(app.record(data), data)

    if base is not None:
        from background_audio import save_mix
        mixed = save_mix(app, base, [*base['layers'], layer], [*base['tracks'], track],
                         lambda _message: None, pending_source=source if pending else None,
                         validate_commit=validate_commit)
        return {key: value for key, value in mixed.items() if key != 'path'}

    preview_id = uuid4().hex
    preview_path = app.audio.root / (preview_id + '.flac')
    try:
        store = SimpleNamespace(asset={source['id']: source}.__getitem__) if pending else app.store
        pcm, rendered = render_layers([layer], [track], store)
        if pending:
            finish_source(source, rendered['layers'])
        encode_preview_pcm(pcm, preview_path, channels=rendered['channels'])
        with preview_path.open('rb') as file:
            digest = hashlib.file_digest(file, 'sha256').hexdigest()
        preview = {
            **identity, **rendered, 'id': preview_id, 'kind': 'assembly',
            'path': str(preview_path), 'sha256': digest, 'sample_rate': RATE,
            'timing_source': 'unavailable',
        }
        with app.store.lock:
            validate_commit()
            app.store.add_assets([source, preview] if pending else [preview])
        return {key: value for key, value in preview.items() if key != 'path'}
    except Exception:
        preview_path.unlink(missing_ok=True)
        raise


def check_revisions(record, data):
    if (data.get('source_revision'), data.get('source_metadata_revision')) != (
            record['revision'], record.get('metadata_revision')):
        raise ValueError('The dialogue changed. Reopen the recording controls.')


def finish_source(source, layers):
    placed = next(layer for layer in layers if layer['asset_id'] == source['id'])
    frames = placed['original_sample_count']
    if frames <= 0:
        raise ValueError('The recording contains no audio samples.')
    placed.update(source_in_sample=0, source_out_sample=frames)
    source.update(sample_count=frames, seconds=frames / RATE)
