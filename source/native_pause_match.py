"""Match quiet handles at a verified native replacement without moving fixed cuts."""

from layered_audio import RATE
from timed_contextual_packets import quiet_edge_padding


THRESHOLD_DBFS = -50
FRAME = RATE // 100


def _padding(pcm, region, threshold):
    start, end = region
    if not 0 <= start < end <= len(pcm):
        raise ValueError('The saved pause range is outside its recording.')
    center = (start + end) // 2
    left, right = (quiet_edge_padding(pcm, edge, center, threshold) for edge in region)
    # A quiet or very short region gives no evidence of a speech edge.
    if end - start - left - right < 2 * FRAME or max(map(abs, pcm[start+left:end-right])) < .01:
        raise ValueError('There is no clear speech edge for matching these pauses.')
    return [left, right]


def measure(source_pcm, source_range, return_pcm, selected_range, sides, *, threshold=THRESHOLD_DBFS,
            minimum_padding=FRAME):
    """Shorten only measured quiet in the selected interval, with a quiet handle."""
    source_quiet = _padding(source_pcm, source_range, threshold)
    return_quiet = _padding(return_pcm, selected_range, threshold)
    removed = [max(0, available - max(reference, minimum_padding)) if enabled else 0
               for available, reference, enabled in zip(return_quiet, source_quiet, sides)]
    if not any(removed):
        raise ValueError('No excess quiet was found at these joins.')
    start, end = selected_range
    return [start + removed[0], end - removed[1]], {
        'threshold_dbfs': threshold, 'source_padding_samples': source_quiet,
        'return_padding_samples': return_quiet, 'removed_samples': removed,
    }


def match_proposal(source_pcm, return_pcm, proposal, threshold):
    """Trim confirmed quiet outside the proposed word interval on each side."""
    if proposal.get('pause_match'):
        return proposal
    words = proposal['replacement_words']
    if not words:
        return proposal
    source_start, source_end = proposal['source_region']
    insert_start, insert_end = proposal['insert_region']
    try:
        _, details = measure(source_pcm, proposal['source_region'], return_pcm,
                             proposal['insert_region'], (source_start > 0, source_end < len(source_pcm)),
                             threshold=min(-55, threshold), minimum_padding=3 * FRAME)
    except ValueError:
        return proposal
    # Word timestamps can extend into the measured quiet. Keep that audio
    # unless the existing verified proposal has already narrowed its extent.
    first = round(words[0]['start'] * RATE)
    last = round(words[-1]['end'] * RATE)
    lead, tail = (min(trim, max(0, available)) for trim, available in
                  zip(details['removed_samples'], (first, insert_end - insert_start - last)))
    lead = lead if lead >= 2 * FRAME else 0
    tail = tail if tail >= 2 * FRAME else 0
    if not (lead or tail):
        return proposal
    adjusted_words = [{**word, **{edge: (round(word[edge] * RATE) - lead) / RATE
                                  for edge in ('start', 'end')}} for word in words]
    return {**proposal, 'insert_region': [insert_start + lead, insert_end - tail],
            'replacement_words': adjusted_words,
            'excluded_return_samples': proposal['excluded_return_samples'] + lead + tail,
            'pause_match': {**details, 'removed_samples': [lead, tail]}}


def for_boundary(app, record, data, piece, parent, pcm, selection):
    """Use the source interval belonging to the current timeline's fixed clips."""
    from clip_boundaries import check_source_owner, digest

    base = app.store.asset(data['base_asset_id'])
    proposal = base.get('join_proposal') or {}
    region = proposal.get('source_region')
    target_id = data['layer_id']
    layers = {layer['id']: layer for layer in base.get('layers', [])}
    target = layers.get(target_id, {})
    before, after = layers.get('source-before'), layers.get('source-after')
    if (not piece.get('native_join_slice') or target_id != 'proposed-replacement'
            or target.get('asset_id') != data['piece_id']
            or base.get('record_id') != f"{record['dataset']}:{record['index']}"
            or base.get('source_revision') != record['revision']
            or base.get('source_metadata_revision') != record['metadata_revision']
            or base.get('replacement_playback') or not isinstance(region, list) or len(region) != 2):
        raise ValueError('Open the current native replacement in its dialogue mix to match pauses.')
    if not 0 <= selection[0] < selection[1] <= len(pcm):
        raise ValueError('Choose an ordered source sample range within the take.')
    saved = piece['native_join_slice']['return_samples']
    if not saved[0] <= selection[0] < selection[1] <= saved[1]:
        raise ValueError('Match pauses within the currently saved replacement interval.')
    if (target.get('source_in_sample') != 0 or target.get('source_out_sample') != piece['sample_count']
            or target.get('playback_speed', 1) != 1):
        raise ValueError('Restore the replacement’s saved range and speed before matching pauses.')

    source = app.store.asset(piece['source_take_asset_id'])
    check_source_owner(record, source)
    if (source.get('sha256') != parent['native_context_window']['source_sha256']
            or digest(source['path']) != source['sha256'] or source.get('channels') != 1
            or source.get('sample_rate') != RATE):
        raise ValueError('The original recording is unavailable or has changed.')
    from replacement_snap import automatic_join_sides
    automatic = automatic_join_sides(base['layers'], target)
    sides = [bool(before), bool(after)]
    for side, neighbor, edge, sample, outgoing, incoming in (
        ('lead', before, 'source_out_sample', region[0], before, target),
        ('tail', after, 'source_in_sample', region[1], target, after),
    ):
        if neighbor is None:
            continue
        if (neighbor.get('asset_id') != source['id'] or neighbor.get(edge) != sample
                or neighbor.get('playback_speed', 1) != 1
                or incoming.get('after_layer_id') != outgoing['id']
                or not automatic[side]):
            raise ValueError('Restore the original linked joins before matching pauses.')
    from audio import decode
    bounds, proof = measure(decode(source['path']), region, pcm, selection, sides)
    return bounds, {**proof, 'base_asset_id': base['id']}
