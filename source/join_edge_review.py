"""Flag abrupt steps and simultaneous clip activity at speech joins."""

from copy import deepcopy
from statistics import median

from guide_bindings import RATE, guide, parent_id

NEARBY_FRAMES = round(RATE * .002)
MIN_STEP = .04
MIN_RATIO = 8
FADE_FRAMES = round(RATE * .001)
# The stored reference mix is quantized to 24-bit FLAC.
PCM_TOLERANCE = 2 ** -22
ACTIVITY_WINDOW = round(RATE * .01)
ACTIVITY_REVIEW_LIMIT = round(RATE * .35)
ACTIVITY_THRESHOLD_DBFS = -40
MIN_SHARED_WINDOWS = 2


def _edge_step(pcm, channels, frame):
    """Return the largest channel step that stands out from nearby samples."""
    frames = len(pcm) // channels
    if not 1 <= frame < frames:
        return 0
    start = max(1, frame - NEARBY_FRAMES)
    stop = min(frames, frame + NEARBY_FRAMES + 1)
    steps = []
    for channel in range(channels):
        def difference(at):
            index = at * channels + channel
            return abs(pcm[index] - pcm[index - channels])

        step = difference(frame)
        if step < MIN_STEP:
            continue
        nearby = [difference(at) for at in range(start, stop) if at != frame]
        if nearby and step >= MIN_RATIO * median(nearby):
            steps.append(step)
    return max(steps, default=0)


def speech_clip_audible(layer, track, any_solo):
    return (layer['kind'] == 'audio' and layer.get('rendered_sample_count', 0) > 0
            and layer.get('clip_role') not in {'background_music', 'background_ambience', 'imported_audio'}
            and track.get('role') not in {'music', 'ambience'}
            and not (layer.get('muted') or track.get('muted') or any_solo and not track.get('solo')))


def activity_join_bounds(prior, incoming):
    """Estimated activity cutoff and leader, in timeline samples."""
    if (incoming['start_sample'] < prior['start_sample']
            or any(layer.get('rendered_sample_count', 0) <= 0 for layer in (prior, incoming))):
        return None
    try:
        cutoff = guide(prior, 'decay')
        leader = guide(incoming, 'leader')
    except ValueError:
        return None
    if any(point.get('kind') != 'activity' or point.get('timing_partial') for point in (cutoff, leader)):
        return None
    return prior['start_sample'] + cutoff['sample'], incoming['start_sample'] + leader['sample']


def _continuous_source_join(prior, incoming):
    """Skip a plain split that still follows the original recording."""
    source_out = prior.get('source_out_sample', prior.get('original_sample_count'))
    return (prior['asset_id'] == incoming['asset_id']
            and prior['end_sample'] == incoming['start_sample']
            and source_out == incoming.get('source_in_sample', 0)
            and all(clip.get('effective_speed', clip.get('playback_speed', 1)) == 1
                    and not clip.get('rendered_region') for clip in (prior, incoming)))


def mark_join_edges(pcm, channels, layers, audible_speech):
    """Attach review hints from this mix, discarding hints from older renders."""
    by_id = {layer['id']: layer for layer in layers}
    for layer in layers:
        layer.pop('join_edge_warning', None)
        if layer['id'] not in audible_speech:
            continue
        prior = by_id.get(parent_id(layer))
        if (not prior or prior['id'] not in audible_speech
                or not layer.get('asset_id') or not prior.get('asset_id')
                or _continuous_source_join(prior, layer)):
            continue
        frames = {layer['start_sample'], prior['end_sample']}
        step, frame = max((_edge_step(pcm, channels, frame), frame) for frame in frames)
        if step:
            layer['join_edge_warning'] = {'sample': frame, 'step': round(step, 6)}


def mark_join_activity(layers, chunks, tracks, audible_speech):
    """Review the first 350 ms of shared time using post-clip, pre-bus signal."""
    by_id = {layer['id']: layer for layer in layers}
    threshold_power = 10 ** (ACTIVITY_THRESHOLD_DBFS / 10)

    def active(layer, start):
        channels = layer['source_channels']
        begin = (start - layer['start_sample']) * channels
        length = ACTIVITY_WINDOW * channels
        signal = memoryview(chunks[layer['id']])[begin:begin + length]
        track_power = 10 ** (tracks[layer['track_id']].get('gain_db', 0) / 10)
        return sum(sample * sample for sample in signal) * track_power >= threshold_power * length

    for layer in layers:
        layer.pop('join_activity_warning', None)
        prior = by_id.get(parent_id(layer))
        if (not prior or layer['id'] not in audible_speech or prior['id'] not in audible_speech
                or any(clip.get('track_bus_id') for clip in (prior, layer))
                or not layer.get('asset_id') or not prior.get('asset_id')
                or prior['asset_id'] == layer['asset_id']):
            continue
        start = max(prior['start_sample'], layer['start_sample'])
        end = min(prior['end_sample'], layer['end_sample'], start + ACTIVITY_REVIEW_LIMIT)
        run = longest = 0
        for window in range(start, end - ACTIVITY_WINDOW + 1, ACTIVITY_WINDOW):
            run = run + 1 if active(prior, window) and active(layer, window) else 0
            longest = max(longest, run)
        if longest >= MIN_SHARED_WINDOWS:
            layer['join_activity_warning'] = {
                'shared_samples': longest * ACTIVITY_WINDOW,
                'threshold_dbfs': ACTIVITY_THRESHOLD_DBFS,
            }


def _join_fade_edges(base, preceding, incoming, frame):
    """Return only the clip edges this comparison is allowed to change."""
    preserve_base = (base.get('contextual_assembly') or {}).get('preserve_base') is True
    candidates = ((preceding, 'end_sample', 'fade_out_samples'),
                  (incoming, 'start_sample', 'fade_in_samples'))
    for layer, position, fade in candidates:
        fixed = layer.get('track_id') == 'base' or layer.get('slot', {}).get('type') == 'fixed'
        if layer[position] == frame and not (preserve_base and fixed):
            yield layer, fade


def prepare_join_fade(base, incoming_id):
    """Propose a short taper at the clip edges that meet the flagged sample."""
    layers = deepcopy(base['layers'])
    by_id = {layer['id']: layer for layer in layers}
    incoming = by_id.get(incoming_id) if isinstance(incoming_id, str) else None
    preceding = by_id.get(parent_id(incoming)) if incoming else None
    warning = incoming.get('join_edge_warning') if incoming else None
    message = 'This join needs a manual adjustment in Track and Clip Controls.'
    if not base.get('join_edges_checked') or not warning or not preceding:
        raise ValueError(message)
    frame = warning['sample']
    if (not FADE_FRAMES <= frame <= base['sample_count'] - FADE_FRAMES
            or any(layer['kind'] != 'audio' or not layer.get('asset_id')
                   or layer.get('rendered_region')
                   or layer['end_sample'] - layer['start_sample'] < 2 * FADE_FRAMES
                   for layer in (preceding, incoming))):
        raise ValueError(message)
    edges = list(_join_fade_edges(base, preceding, incoming, frame))
    if not edges or any(layer.get(fade) for layer, fade in edges):
        raise ValueError(message)
    for layer, fade in edges:
        layer[fade] = FADE_FRAMES
    return layers, frame


def validate_join_fade(base, original_pcm, pcm, result, frame, incoming_id):
    """Check that the taper changes only its two short edge windows."""
    def positions(asset):
        return {layer['id']: (layer['start_sample'], layer['end_sample']) for layer in asset['layers']}

    channels = result['channels']
    if (channels != base['channels'] or result['sample_count'] != base['sample_count']
            or len(pcm) != len(original_pcm) or positions(base) != positions(result)):
        raise ValueError('The fade would move clips or change the duration.')
    incoming = next(layer for layer in result['layers'] if layer['id'] == incoming_id)
    before_step = _edge_step(original_pcm, channels, frame)
    index = frame * channels
    after_step = max(abs(pcm[index + channel] - pcm[index + channel - channels])
                     for channel in range(channels))
    if incoming.get('join_edge_warning') or not before_step or after_step > before_step / 2:
        raise ValueError('The fade could not resolve the measured click.')
    original_layers = {layer['id']: layer for layer in base['layers']}
    original_incoming = original_layers[incoming_id]
    preceding = original_layers[parent_id(original_incoming)]
    allowed = {fade for _, fade in _join_fade_edges(base, preceding, original_incoming, frame)}
    start = (frame - (FADE_FRAMES if 'fade_out_samples' in allowed else 0)) * channels
    end = (frame + (FADE_FRAMES if 'fade_in_samples' in allowed else 0)) * channels
    for index, (before, after) in enumerate(zip(original_pcm, pcm)):
        if not start <= index < end and abs(before - after) > PCM_TOLERANCE:
            raise ValueError('The fade would also change audio away from the join.')
