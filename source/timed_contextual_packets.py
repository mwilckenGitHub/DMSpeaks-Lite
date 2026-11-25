"""Propose audition packets from a timed source and saved contextual returns."""
import hashlib
import json
import math

from alignment_backend import source_alignment_fingerprint
from contextual_layer_layout import RATE, _validate_asset
from contextual_packet_layout import packet_slices
from engine_limits import MAX_SEGMENTS
from model import char_word_mapping
from performance_plan import parse_record
from phoneme_snap import PhonemeJoinRejected
from speech_edit_authoring import WORDS, delimiter_text_spans
from spoken_spans import normalized_spoken_spans

DEFAULT_CONTEXT_WORDS = 3


def _literal_marker(segment):
    marker = segment.get('placeholder')
    return bool(marker and segment.get('value') == marker and not segment.get('literal_suffix')
                and not segment.get('speech_excluded'))


def _native_preview_form(app, preview_id, identity, available, native_returns):
    """Recover the native return and its original assembly options by saved IDs."""
    from contextual_delivery import NATIVE_DIALOGUE_MIX

    if not isinstance(preview_id, str):
        return None
    try:
        preview = app.store.asset(preview_id)
        if ((preview.get('contextual_assembly') or {}).get('format') != NATIVE_DIALOGUE_MIX
                or tuple(preview.get(field) for field in (
                    'record_id', 'source_revision', 'source_metadata_revision', 'sample_rate')) != identity):
            return None
        layer = next((item for item in preview.get('layers', [])
                      if item['id'] == 'proposed-replacement'), None)
        if not layer or not preview.get('join_proposal'):
            return None
        clip = app.store.asset(layer['asset_id'])
    except ValueError:
        return None

    baseline = clip.get('source_take_asset_id')
    piece = clip.get('source_clip_id')
    cut = clip.get('native_join_slice') or {}
    order = layer['slot_order']
    value = layer['slot'].get('value')
    binding = (baseline, piece, cut.get('return_sha256'))
    if not piece or not cut.get('return_sha256') or not any(
            item['baseline'] and item['id'] == baseline for item in available):
        return None
    match = next((item for item in available
                  if item['values'].get(order) == value
                  and native_returns.get(item['id']) == binding), None)
    if not match:
        return None
    settings = preview['join_proposal'].get('assembly_settings')
    settings = settings if isinstance(settings, dict) else {}
    policy = layer.get('replacement_snap', {})
    match_padding = settings.get('match_padding', bool(policy.get('preserve_source_padding')))
    min_dbfs = settings.get('min_dbfs', -45)
    if type(match_padding) is not bool or type(min_dbfs) not in (int, float) or not -75 <= min_dbfs <= -30:
        return None
    result = {'source_asset_id': baseline, 'return_asset_id': match['id'], 'slot_order': order,
              'value': value, 'match_padding': match_padding, 'min_dbfs': min_dbfs}
    if 'snap_mode' in settings or 'mode' in policy:
        result['snap_mode'] = settings.get('snap_mode', policy.get('mode'))
    return result


def _declared_values(generation, occurrences):
    """Read requested values for current occurrences from the saved generation trace."""
    mode = generation.get('mode')
    if mode not in ('context', 'direct', 'native_context'):
        return {}
    segments = generation.get('segments')
    if not isinstance(segments, list):
        return {}
    values = {}
    for segment in segments:
        if not isinstance(segment, dict) or type(segment.get('order')) is not int:
            continue
        order = segment['order']
        if mode != 'context' and generation.get('slot_order') != order:
            continue
        occurrence = occurrences.get(order)
        value = segment.get('value')
        if (not occurrence or segment.get('speech_excluded') or not isinstance(value, str)
                or not value.strip() or any(segment.get(key, '') != occurrence.get(key, '')
                                            for key in ('type', 'placeholder', 'literal_suffix'))):
            continue
        values[order] = value
    return values


def _recorded_values(asset, generation, occurrences):
    """Expose declared values only when their saved word intervals match."""
    declared = _declared_values(generation, occurrences)
    if not declared:
        return {}
    timed_by_order = {}
    for word in asset['words']:
        if (isinstance(word, dict) and type(word.get('segment_order')) is int
                and isinstance(word.get('word'), str)
                and type(word.get('start')) in (int, float) and type(word.get('end')) in (int, float)
                and 0 <= word['start'] < word['end'] <= asset['sample_count'] / RATE):
            timed_by_order.setdefault(word['segment_order'], []).extend(_tokens(word['word']))
    return {order: value for order, value in declared.items()
            if _tokens(value + (occurrences[order].get('literal_suffix') or '')) == timed_by_order.get(order)}


def saved_fallbacks(store, source, slot, value, excluded):
    """Find declared defaults recorded for this exact take and occurrence."""
    from content_review import is_rejected

    with store.lock:
        rows = store.db.execute("""
            SELECT metadata FROM assets
            WHERE json_valid(metadata)
              AND json_extract(metadata, '$.record_id') = ?
              AND json_extract(metadata, '$.source_revision') = ?
              AND json_extract(metadata, '$.source_metadata_revision') IS ?
              AND json_extract(metadata, '$.source_take_asset_id') = ?
              AND json_extract(metadata, '$.generation.slot_order') = ?
              AND json_extract(metadata, '$.generation.mode') IN ('direct', 'native_context')
              AND EXISTS (SELECT 1 FROM json_each(metadata, '$.generation.segments') AS segment
                          WHERE CASE WHEN segment.type = 'object'
                                     THEN json_extract(segment.value, '$.value') END = ?)
              AND json_extract(metadata, '$.layers') IS NULL
              AND json_array_length(metadata, '$.words') > 0
            ORDER BY rowid DESC
        """, (source['record_id'], source['source_revision'], source.get('source_metadata_revision'),
              source['id'], slot['order'], value)).fetchall()
    for row, in rows:
        asset = json.loads(row)
        if (asset['id'] == source['id'] or asset['id'] in excluded or is_rejected(asset)
                or asset.get('audition_only') or type(asset.get('sample_count')) is not int
                or asset['sample_count'] <= 0):
            continue
        generation = asset['generation']
        if generation['mode'] == 'native_context':
            receipt = asset.get('native_context_take')
            timing = hashlib.sha256(json.dumps(asset['words'], sort_keys=True).encode()).hexdigest()
            if (not isinstance(receipt, dict) or receipt.get('context_sha256') != asset.get('sha256')
                    or receipt.get('timing_sha256') != timing):
                continue
        if _recorded_values(asset, generation, {slot['order']: slot}).get(slot['order']) == value:
            yield asset


def recordings(app, data):
    """List this entry's current mono recordings with saved timing labels."""
    from content_review import is_rejected
    record = app.record(data)
    occurrences = {segment['order']: segment for segment in parse_record(record)['segments']
                   if segment['type'] != 'fixed' and not segment.get('speech_excluded')}
    identity = (f"{record['dataset']}:{record['index']}", record['revision'],
                record.get('metadata_revision'), RATE)
    current_recording = """
            json_valid(metadata)
              AND json_extract(metadata, '$.record_id') = ?
              AND json_extract(metadata, '$.source_revision') = ?
              AND json_extract(metadata, '$.source_metadata_revision') IS ?
              AND json_extract(metadata, '$.layers') IS NULL
              AND json_extract(metadata, '$.sample_rate') = ?
              AND coalesce(json_extract(metadata, '$.channels'), 1) = 1
    """
    with app.store.lock:
        rows = app.store.db.execute(f"""
            SELECT metadata FROM assets WHERE {current_recording}
              AND json_array_length(metadata, '$.words') > 0
            ORDER BY rowid DESC LIMIT 100
        """, identity).fetchall()
    available = []
    native_returns = {}
    preview_id = data.get('preview_asset_id')
    for (metadata,) in rows:
        asset = json.loads(metadata)
        if is_rejected(asset) or asset.get('audition_only'):
            continue
        labels = [str(word.get('word', '')) for word in asset['words'][:12] if isinstance(word, dict)]
        generation = asset.get('generation') or {}
        count = asset.get('sample_count')
        if type(count) is not int or count <= 0 or not isinstance(generation, dict):
            continue
        baseline = asset.get('kind') == 'take' and generation.get('mode') == 'context'
        values = _recorded_values(asset, generation, occurrences)
        full_return = None
        if baseline and isinstance(generation.get('adapter_input_text'), str):
            try:
                tokens, times = _timed_words(asset)
                full_return = _verified_full_return(record, asset, tokens)
            except ValueError:
                full_return = None
            if full_return is not None:
                ranges, verified_values = full_return
                values = {order: verified_values[order] for order, (first, last) in ranges.items()
                          if order in occurrences and all(start < end for start, end in times[first:last])}
        entry = dict(id=asset['id'], label=asset.get('name') or ' '.join(labels),
                     seconds=count / RATE, baseline=baseline, values=values,
                     audio_sha256=asset.get('sha256'))
        try:
            entry['alignment_sha256'] = source_alignment_fingerprint(asset)
        except (AttributeError, TypeError, ValueError):
            # Older malformed clocks can still appear in the recording list.
            # They cannot substantiate a saved manual cut.
            pass
        if full_return is not None and not asset.get('native_context_take'):
            entry['full_return_timing_review_available'] = True
        adapter_input = generation.get('adapter_input_text')
        if baseline and isinstance(adapter_input, str) and adapter_input.strip():
            entry['timing_review_available'] = True
            if any(slot.get('placeholder') and slot['placeholder'] in adapter_input for slot in occurrences.values()):
                parsed = parse_record(record, asset.get('player', {}), {
                    'stage_directions': generation.get('stage_direction_format', 'auto')})
                segments = parsed['segments']
                if adapter_input == ''.join(segment['resolved_text'] for segment in segments):
                    literal_slots = [segment['order'] for segment in segments if _literal_marker(segment)]
                    if literal_slots:
                        entry['literal_slots'] = literal_slots
        available.append(entry)
        receipt = asset.get('native_context_take')
        if preview_id and isinstance(receipt, dict):
            native_returns[asset['id']] = (asset.get('source_take_asset_id'),
                                            asset.get('source_clip_id'), receipt.get('insert_sha256'))
    if data.get('include_timing_status') is not True:
        return available

    result = {'recordings': available}
    native = _native_preview_form(app, preview_id, identity, available, native_returns)
    if native is not None:
        result['native_preview_form'] = native
    has_baseline = any(take['baseline'] for take in available)
    untimed_recording = f"""{current_recording}
              AND coalesce(json_array_length(metadata, '$.words'), 0) = 0
              AND coalesce(json_extract(metadata, '$.content_review.status'), '') != 'rejected'
              AND NOT coalesce(json_extract(metadata, '$.audition_only'), 0)
    """
    with app.store.lock:
        candidate_query = f"""
            SELECT id, json_extract(metadata, '$.name'),
                json_extract(metadata, '$.generation.spoken_text'),
                json_extract(metadata, '$.sample_count'),
                json_extract(metadata, '$.generation')
            FROM assets WHERE {untimed_recording}
              AND json_extract(metadata, '$.kind') = 'take'
              AND json_type(metadata, '$.generation.spoken_text') = 'text'
              AND length(trim(json_extract(metadata, '$.generation.spoken_text'))) > 0
              AND json_type(metadata, '$.generation.adapter_input_text') = 'text'
              AND length(trim(json_extract(metadata, '$.generation.adapter_input_text'))) > 0
              AND json_type(metadata, '$.sample_count') = 'integer'
              AND json_extract(metadata, '$.sample_count') > 0
        """
        untimed = 0
        direct_takes = []
        full_takes = app.store.db.execute(candidate_query + """
            AND json_extract(metadata, '$.generation.mode') = 'context'
            ORDER BY rowid DESC LIMIT 25
        """, identity).fetchall()
        if not has_baseline:
            untimed = app.store.db.execute(f"""
                SELECT count(*) FROM assets WHERE {untimed_recording}
            """, identity).fetchone()[0]
        elif occurrences:
            slots = ', '.join('?' for _ in occurrences)
            direct_takes = app.store.db.execute(candidate_query + f"""
                AND json_extract(metadata, '$.generation.mode') = 'direct'
                AND json_type(metadata, '$.generation.slot_order') = 'integer'
                AND json_extract(metadata, '$.generation.slot_order') IN ({slots})
                ORDER BY rowid DESC LIMIT 25
            """, (*identity, *occurrences)).fetchall()

    def choices(takes):
        result = []
        for ident, label, transcript, count, generation in takes:
            item = {'id': ident, 'label': label or ' '.join(transcript.split()[:12]),
                    'seconds': count / RATE}
            values = _declared_values(json.loads(generation), occurrences)
            if values:
                item['declared_values'] = values
            result.append(item)
        return result

    result.update(untimed=untimed, alignable_full_takes=choices(full_takes))
    if direct_takes:
        result['alignable_direct_takes'] = choices(direct_takes)
    return result


def _tokens(text):
    return [match[0].replace('’', "'").casefold() for match in WORDS.finditer(text)]


def _timed_words(asset):
    _, count = _validate_asset(asset, 'timed recording')
    words = asset.get('words')
    if not isinstance(words, list) or not words:
        raise ValueError('This recording has no saved word timings.')
    tokens, spans = [], []
    prior_start = prior_end = 0
    for word in words:
        label = word.get('word') if isinstance(word, dict) else None
        if not isinstance(label, str) or len(keys := _tokens(label)) != 1:
            raise ValueError('Saved word timings need one spoken token per label.')
        start, end = word.get('start'), word.get('end')
        if any(type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= count / RATE
               for t in (start, end)):
            raise ValueError('Saved word timings are outside the decoded recording.')
        start, end = round(start * RATE), round(end * RATE)
        if start < prior_start or end < prior_end or end < start:
            raise ValueError('Saved word timings need to follow the order of the spoken words.')
        tokens.append(keys[0])
        spans.append((start, end))
        prior_start, prior_end = start, end
    return tokens, spans


def _verified_full_return(record, context, tokens):
    """Bind a complete saved transcript to its authored occurrence positions."""
    generation = context.get('generation')
    player = context.get('player', {})
    if not isinstance(generation, dict) or generation.get('mode') != 'context' or not isinstance(player, dict):
        return None
    adapter_input = generation.get('adapter_input_text')
    if not isinstance(adapter_input, str):
        return None
    segments = parse_record(record, player, {
        'stage_directions': generation.get('stage_direction_format', 'auto')})['segments']
    if adapter_input != ''.join(segment['resolved_text'] for segment in segments):
        return None
    transcript, mapping = normalized_spoken_spans(adapter_input)
    if transcript != generation.get('spoken_text') or _tokens(transcript) != tokens:
        return None
    words = list(WORDS.finditer(transcript))
    starts = {word.start(): index for index, word in enumerate(words)}
    ends = {word.end(): index + 1 for index, word in enumerate(words)}
    ranges, values = {}, {}
    for segment, _, _, first, last in delimiter_text_spans(segments, transcript, mapping):
        start, end = starts.get(first), ends.get(last)
        if start is not None and end is not None and start < end:
            ranges[segment['order']] = (start, end)
            values[segment['order']] = segment['value']
    return ranges, values


def quiet_seam(pcm, start, end, min_dbfs=-45, *, near='center', require_unique=False):
    """Find a low-energy run between timed words, with a peak guard for clicks."""
    frame = RATE // 100
    energy_limit = 10 ** (min_dbfs / 20)
    peak_limit = energy_limit * 4
    if not 0 <= start < end <= len(pcm):
        raise ValueError('The neighboring word timings leave no independent join.')
    runs, run_start = [], None
    for begin in range(start, end - frame + 1, frame):
        samples = pcm[begin:begin + frame]
        quiet = (max(map(abs, samples)) <= peak_limit and
                 math.sqrt(sum(sample * sample for sample in samples) / frame) <= energy_limit)
        if quiet and run_start is None:
            run_start = begin
        elif not quiet and run_start is not None:
            runs.append((run_start, begin))
            run_start = None
    if run_start is not None:
        runs.append((run_start, start + (end - start) // frame * frame))
    runs = [run for run in runs if run[1] - run[0] >= 4 * frame]
    if not runs:
        raise ValueError('No measured quiet join between the neighboring words.')
    if require_unique and len(runs) != 1:
        raise ValueError('More than one quiet interval could belong to this join.')
    lo, hi = runs[-1] if near == 'end' else runs[0] if near == 'start' else max(
        runs, key=lambda run: run[1] - run[0])
    center = (lo + hi) // 2
    radius = frame // 2
    cut = min(range(center - radius, center + radius + 1), key=lambda i: (abs(pcm[i]), abs(i - center)))
    return cut, dict(quiet_span=[lo, hi], threshold_dbfs=min_dbfs)


def _between_words(pcm, previous, following, threshold, *, near, automatic):
    """Use a short, verified quiet interval when timing labels nearly touch."""
    start, end = previous[1], following[0]
    try:
        return quiet_seam(pcm, start, end, threshold, near=near)
    except ValueError:
        frame = RATE // 100
        if not automatic or not 0 <= end - start < 4 * frame:
            raise
        # Keep the cut between the reported intervals. Samples inside those
        # intervals corroborate the seam without moving either word boundary.
        cut = (start + end) // 2
        lo, hi = cut - frame, cut + frame
        if not 0 <= previous[0] <= lo < hi <= following[1] <= len(pcm):
            raise
        level = min(-55, threshold)
        limit = 10 ** (level / 20)
        for begin in (lo, cut):
            samples = pcm[begin:begin + frame]
            if (max(map(abs, samples)) > limit * 3
                    or sum(sample * sample for sample in samples) > frame * limit * limit):
                raise
        return cut, dict(quiet_span=[lo, hi], threshold_dbfs=level, cut_sample=cut,
                         verification='quiet_across_reported_word_boundary')


class _JoinBoundaryError(ValueError):
    """A measured boundary failed on a specific recording and side."""

    def __init__(self, recording, edge, reason, window, *, pending_lead=None):
        self.recording = recording
        self.edge = edge
        self.window = window
        self.pending_lead = pending_lead
        label = 'Base recording' if recording == 'base' else 'Replacement recording'
        super().__init__(f'{label}, {edge}: {reason}')


def _hold_reason(error, *, prefix=''):
    """Save diagnostic data without retaining tracebacks and their audio buffers."""
    reason = {'reason': str(error).removeprefix(prefix)}
    if isinstance(error, PhonemeJoinRejected):
        reason['code'] = 'phoneme_join_rejected'
    if isinstance(error, _JoinBoundaryError):
        reason.update(join_recording=error.recording, join_edge=error.edge, join_window=error.window)
        if error.pending_lead:
            key = 'pending_manual_base_cut' if error.recording == 'base' else 'pending_manual_return_cut'
            reason[key] = error.pending_lead
        if getattr(error, 'full_return_timing_review_available', False):
            reason['full_return_timing_review_available'] = True
        if hasattr(error, 'manual_return_cut'):
            reason['manual_return_cut'] = error.manual_return_cut
            if hasattr(error, 'manual_return_bounds'):
                reason['manual_return_bounds'] = error.manual_return_bounds
    return reason


def _manual_cuts(raw, *, allowed, binding, invalid_message, stale_message):
    """Accept a legacy single cut or one cut for each edge."""
    cuts = raw if isinstance(raw, list) else [raw]
    if not allowed or not 1 <= len(cuts) <= 2 or any(
            not isinstance(cut, dict) or set(cut) != {*binding, 'sample', 'edge'}
            or type(cut['sample']) is not int or cut['edge'] not in ('lead-in', 'tail')
            for cut in cuts):
        raise ValueError(invalid_message)
    by_edge = {cut['edge']: cut for cut in cuts}
    if len(by_edge) != len(cuts):
        raise ValueError('Choose at most one manual cut for each edge.')
    if any(cut[key] != value for cut in cuts for key, value in binding.items()):
        raise ValueError(stale_message)
    return by_edge


def _window(pcm, spans, lo, hi, threshold, *, recording, automatic=False, manual_cuts=None,
            audio_bounds=None):
    """Find the start and end of the owned words in this recording's clock."""
    cuts = manual_cuts or {}
    used_manual_cuts = set()
    lower, upper = audio_bounds or (0, len(pcm))

    def cut(index, edge, near):
        previous_start, previous_end = spans[index - 1]
        following_start, following_end = spans[index]
        previous = max(lower, previous_start), max(lower, previous_end)
        following = min(upper, following_start), min(upper, following_end)
        try:
            return _between_words(pcm, previous, following, threshold, near=near, automatic=automatic)
        except ValueError as error:
            gap = following[0] - previous[1]
            if manual_cut := cuts.get(edge):
                sample = manual_cut['sample']
                if not previous_end < sample < following_start:
                    label = 'base' if recording == 'base' else 'replacement'
                    raise ValueError(f'The manual {label} cut must be between the saved word times.') from error
                if not lower <= sample <= upper:
                    raise ValueError('The replacement cut must stay within the saved generated Fish audio.') from error
                used_manual_cuts.add(edge)
                return sample, dict(cut_sample=sample, verification='manual_connected_cut',
                                    word_gap=[previous_end, following_start])
            reason = str(error)
            if gap < 0:
                reason = f'The reported word times overlap by {-gap * 1000 / RATE:.1f} ms.'
            elif gap == 0:
                reason = ('The reported word times meet, and the waveform does not confirm a quiet cut.'
                          if automatic else 'The reported word times meet, leaving no interval to check.')
            window = dict(previous_end=previous[1], following_start=following[0], sample_count=len(pcm))
            pending_lead = cuts['lead-in'] if 'lead-in' in used_manual_cuts else None
            raise _JoinBoundaryError(recording, edge, reason, window, pending_lead=pending_lead) from error

    left = cut(lo, 'lead-in', 'end') if lo else (lower, {'edge': 'start'})
    right = cut(hi, 'tail', 'start') if hi < len(spans) else (upper, {'edge': 'end'})
    if cuts.keys() != used_manual_cuts:
        label = 'base' if recording == 'base' else 'replacement'
        raise ValueError(f'This {label} edge no longer needs a manual cut. Clear the cut and build again.')
    return [left[0], right[0]], [left[1], right[1]]


def _clip_roles(packet_id, choice):
    lo, hi = choice['returned_region']
    return packet_slices(packet_id, (lo, *choice['word_bounds'], hi)).keys()


def _quiet_handles(pcm, region, words, before, after, threshold):
    """Identify measured silence which may stay outside a delimiter clip."""
    # The join threshold may be raised in the UI. Omitting a handle uses a
    # stricter floor and checks peaks throughout the entire handle.
    level = min(-55, threshold)
    energy = 10 ** (level / 20)
    for side, count in enumerate((before, after)):
        lo, hi = sorted((region[side], words[side]))
        if count or lo == hi:
            continue
        samples = pcm[lo:hi]
        if (max(map(abs, samples)) <= energy * 3
                and math.sqrt(sum(sample * sample for sample in samples) / len(samples)) <= energy):
            yield side, dict(quiet_handle=[lo, hi], threshold_dbfs=level, cut_sample=words[side])


def quiet_edge_padding(pcm, edge, word_edge, threshold):
    """Count contiguous quiet samples from the recording edge toward the word."""
    frame = RATE // 100
    step = frame if edge < word_edge else -frame
    limit = 10 ** (threshold / 20)
    at = edge
    while frame <= abs(word_edge - at) and abs(at - edge) < 2 * RATE:
        lo, hi = sorted((at, at + step))
        samples = pcm[lo:hi]
        if (max(map(abs, samples)) > limit * 3
                or sum(sample * sample for sample in samples) > frame * limit * limit):
            break
        at += step
    return abs(at - edge)


def _match_edge_padding(region, joins, pcm, spans, start, end, source_padding, threshold,
                        *, allow_contextual_cuts=False):
    """Trim excess verified quiet between a return's cut and its timed words."""
    frame = RATE // 100
    threshold = min(-55, threshold)
    for side, word_edge in enumerate((spans[start][0], spans[end - 1][1])):
        seam = joins[side]
        edge = region[side]
        direction = 1 if side == 0 else -1
        contextual = 'edge' not in seam
        if contextual:
            if not allow_contextual_cuts or 'quiet_span' not in seam:
                continue
            lo, hi = seam['quiet_span']
            if not lo <= edge <= hi:
                continue
            # Stop at the first limit: the verified quiet interval or the word.
            word_edge = min(word_edge, hi) if side == 0 else max(word_edge, lo)
        if direction * (word_edge - edge) <= 0:
            continue
        quiet = quiet_edge_padding(pcm, edge, word_edge, threshold)
        retain = max(source_padding[side], 3 * frame)
        trim = quiet - retain
        if trim < 2 * frame:
            continue
        if contextual:
            # Find a low-amplitude retained sample within 1 ms of the target without
            # exceeding the verified trim or the retained pause budget.
            trim = min(range(max(2 * frame, trim - RATE // 1000), trim + 1),
                       key=lambda count: (abs(pcm[edge + direction * count - side]), -count))
        region[side] = edge + direction * trim
        joins[side] = {**seam, 'quiet_padding_removed_samples': trim,
                       'quiet_padding_threshold_dbfs': threshold, 'cut_sample': region[side]}
        if not contextual:
            joins[side]['threshold_dbfs'] = threshold


def _source_padding(pcm, region, spans, lo, hi, threshold):
    edges = spans[lo][0], spans[hi - 1][1]
    return [quiet_edge_padding(pcm, cut, edge, min(-55, threshold)) for cut, edge in zip(region, edges)]


def _choose_packet_windows(choices_by_order, *, partial=False):
    """Maximize covered returns, then minimize added context, without overlapping cuts."""
    paths = [((0, 0, 0), 0, [])]
    for _, options in sorted(choices_by_order, key=lambda item: item[0]):
        next_paths = list(paths) if partial else []
        for cost, packet, detail in options:
            lo, hi = packet['source_region']
            compatible = [path for path in paths if path[1] <= lo]
            if compatible:
                (covered, literal, total), _, selected = min(compatible, key=lambda path: path[0])
                accepted = len(packet['alternatives']) if partial else 0
                safe_literal = partial and detail.get('literal_silence_eligible', False)
                next_paths.append(((covered - accepted, literal - safe_literal, total + cost),
                                   hi, [*selected, (packet, detail)]))
        if not next_paths:
            raise ValueError('The measured context windows overlap. Shorten or review the neighboring occurrences.')
        # A path with a later seam and an equal or worse score cannot help.
        paths = []
        for path in sorted(next_paths, key=lambda item: (item[1], item[0])):
            if not paths or path[0] < paths[-1][0]:
                paths.append(path)
    _, _, selected = min(paths, key=lambda path: path[0])
    return [packet for packet, _ in selected], [detail for _, detail in selected]


class _PlanningAlignment:
    """Cache verified evidence and derive missing clocks without changing saved assets."""

    def __init__(self, record, align):
        self.record = record
        self.align = align
        self.cache = {}
        self.derived = []

    def evidence(self, asset, transcript):
        from phoneme_alignment import validate

        if not self.align:
            raise ValueError('No phoneme alignment is available for this recording.')
        key = (asset.get('sha256'), asset.get('sample_rate'), asset.get('sample_count'), transcript)
        if key not in self.cache:
            evidence = self.align(asset, transcript)
            validate(evidence, asset, transcript)
            self.cache[key] = evidence
        return self.cache[key]

    def with_missing_clock(self, asset):
        from phoneme_alignment import timeline_words, validate

        words = asset.get('words')
        if words is not None and words != []:
            return asset  # Malformed existing clocks must fail normal validation.
        _validate_asset(asset, 'untimed recording')
        generation = asset.get('generation', {})
        transcript = generation.get('spoken_text') if isinstance(generation, dict) else None
        if (not self.align or not isinstance(transcript, str)
                or _verified_full_return(self.record, asset, _tokens(transcript)) is None):
            raise ValueError('Missing word timings require a verified full-dialogue recording. '
                             'Partial returns need their own timing evidence.')
        evidence = self.evidence(asset, transcript)
        derived = {**asset, 'words': timeline_words(evidence, validate(evidence, asset, transcript)),
                   'timing_source': 'estimated_phoneme_alignment'}
        _timed_words(derived)
        self.derived.append(dict(asset_id=asset['id'], audio_sha256=asset['sha256'],
                                 transcript_sha256=evidence['transcript_sha256'],
                                 provider=evidence['provider'], language=evidence['language'],
                                 phone_set=evidence['phone_set'],
                                 alignment_sha256=source_alignment_fingerprint(derived)))
        return derived


def plan_packets(record, source, specs, assets, read_pcm, *, partial=False, preserve_base=False,
                 find_fallback=None, fallback_values=None, verify_native_cut=None, align_phones=None):
    """Return explicit packet coordinates plus audit evidence, without edits."""
    from contextual_choices import SAVED_FALLBACK, SILENCE_FALLBACK
    alignment = _PlanningAlignment(record, align_phones if preserve_base else None)
    if (source.get('kind') != 'take' or not isinstance(source.get('generation'), dict)
            or source['generation'].get('mode') != 'context'):
        raise ValueError('Choose an original full dialogue take with saved word timings.')
    source_pcm = read_pcm(source)
    parsed = parse_record(record, source.get('player', {}), {
        'stage_directions': source['generation'].get('stage_direction_format', 'auto')})
    segments = parsed['segments']
    requested = {spec.get('slot_order') for spec in specs
                 if type(spec.get('slot_order')) is int}
    by_order = {s['order']: s for s in segments if s['type'] != 'fixed' and not s.get('speech_excluded')}
    if len(requested) != len(specs) or not requested or not requested <= by_order.keys():
        raise ValueError('Choose a spoken delimiter occurrence from the current dialogue.')
    if len(segments) > MAX_SEGMENTS or any('unfinished' in warning.casefold() for warning in parsed['warnings']):
        raise ValueError('Resolve incomplete source markup or too many segments before assembling saved returns.')
    adapter_input = ''.join(s['resolved_text'] for s in segments)
    transcript, mapping = normalized_spoken_spans(adapter_input)
    transcript_tokens = list(WORDS.finditer(transcript))
    if transcript != source['generation'].get('spoken_text'):
        raise ValueError('Baseline transcript differs from the selected source and its original test inputs.')
    source = alignment.with_missing_clock(source)
    source_tokens, source_times = _timed_words(source)
    source_word_spans = char_word_mapping(parsed['resolved_text'], segments)
    if [_tokens(word['word']) for word in source_word_spans] != [[token] for token in source_tokens]:
        # Keep the timing proposal usable if source markup cannot supply word spans.
        source_word_spans = []
    expected_tokens = _tokens(transcript)
    if len(expected_tokens) != len(source_tokens):
        raise ValueError('Word timings refer to a different transcript.')
    changed_labels = {index for index, (expected, actual) in enumerate(zip(expected_tokens, source_tokens))
                      if expected != actual}
    if changed_labels and (not preserve_base or source['generation'].get('adapter_input_text') != adapter_input):
        raise ValueError('Word timings refer to a different transcript.')
    audible_events = [event for event in parsed.get('performance_events', [])
                      if event.get('kind') in ('vocal', 'sound_effect')]
    text_spans, all_intervals, unusable, literal_labels = {}, {}, {}, {}
    marker_bounds = {}
    for segment, _, _, first, last in delimiter_text_spans(segments, transcript, mapping):
        order = segment['order']
        text_spans[order] = first
        marker_bounds[order] = (first, last)
        owned = [index for index, token in enumerate(transcript_tokens)
                 if first < last and token.start() < last and first < token.end()]
        if not owned:
            unusable[order] = 'The chosen delimiter needs a spoken baseline value.'
            continue
        a, b = owned[0], owned[-1] + 1
        # Even an occurrence with unreliable timings owns its words. Keep
        # them on the base when planning windows for other occurrences.
        all_intervals[order] = (a, b)
        if transcript_tokens[a].start() < first or transcript_tokens[b - 1].end() > last:
            unusable[order] = 'The baseline delimiter shares a word with fixed text or another delimiter.'
        elif any(start == end for start, end in source_times[a:b]):
            unusable[order] = 'A baseline delimiter word has no usable audio interval.'
        elif (a in changed_labels and b == a + 1 and segment['value'] == segment['placeholder']
              and not segment.get('literal_suffix')):
            literal_labels[a] = dict(slot_order=order, marker=segment['placeholder'],
                                     recorded_label=source['words'][a]['word'])

    occupied = {index for lo, hi in all_intervals.values() for index in range(lo, hi)}
    if changed_labels:
        # A literal marker can be pronounced differently from its spelling.
        # Fixed words must still match. Require a separate fixed anchor on
        # each available side, so adjacent slots cannot trade word ownership.
        if (changed_labels != literal_labels.keys() or any(
                not (0 < index or index + 1 < len(source_tokens))
                or index - 1 in occupied or index + 1 in occupied for index in changed_labels)):
            raise ValueError('Word timings refer to a different transcript.')

    alignment_sha256 = source_alignment_fingerprint(source)
    source_binding = dict(source_sha256=source['sha256'], alignment_sha256=alignment_sha256)
    source_phones = None
    if preserve_base and align_phones:
        try:
            source_phones = alignment.evidence(source, transcript)
        except ValueError:
            pass  # The existing measured joins can still be proposed.

    def phoneme_option(order):
        from phoneme_snap import source_region

        start, end = marker_bounds[order]
        region = source_region(source_phones, source, transcript, start, end)
        group = dict(id='segment-' + str(order), context_asset_id=source['id'], context_asset=source,
                     source_region=region, returned_region=region, word_bounds=region,
                     slot=by_order[order], alternatives=[])
        detail = dict(slot_order=order, source_outer_joins=[], returns=[],
                      words_before=0, words_after=0, automatic_context=True,
                      phoneme_alignment=True)
        return -1, group, detail

    def propose_source(order, before, after, threshold, automatic, manual_cuts):
        a, b = all_intervals[order]
        lo, hi = a - before, b + after
        if not 0 <= lo < hi <= len(source_times):
            raise ValueError('There are not enough neighboring words for that context window.')
        if any(other != order and max(lo, start) < min(hi, end)
               for other, (start, end) in all_intervals.items()):
            raise ValueError('The requested context includes another delimiter occurrence.')
        # Unspoken occurrences still have a position in the source transcript.
        # Refuse a context range that steps over them.
        char_lo, char_hi = transcript_tokens[lo].start(), transcript_tokens[hi - 1].end()
        if any(other != order and char_lo < start < char_hi for other, start in text_spans.items()):
            raise ValueError('The requested context includes another delimiter occurrence.')
        raw_lo = mapping[char_lo][0] if lo else 0
        raw_hi = mapping[char_hi - 1][1] if hi < len(source_times) else len(parsed['resolved_text'])
        if any(raw_lo < event['adapter_end'] and event['adapter_start'] < raw_hi for event in audible_events):
            raise ValueError('This context includes an audible cue. Keep that cue on its own source track.')
        src_region, seams = _window(source_pcm, source_times, lo, hi, threshold,
                                    recording='base', automatic=automatic, manual_cuts=manual_cuts)
        original = [source_times[a][0], source_times[b - 1][1]]
        group = dict(id='segment-' + str(order), context_asset_id=source['id'], context_asset=source,
                     source_region=src_region, returned_region=src_region,
                     word_bounds=src_region if preserve_base else original,
                     slot=by_order[order], alternatives=[])
        return group, dict(slot_order=order, source_outer_joins=seams, returns=[],
                           words_before=before, words_after=after)

    full_return_ranges = {}
    native_cut_regions = {}

    def propose_return(group, detail, variant, context, words, tokens, times, pcm, threshold, require_roles):
        order, value = detail['slot_order'], variant['value']
        before, after = detail['words_before'], detail['words_after']
        a, b = all_intervals[order]
        expected = [*source_tokens[a-before:a], *words, *source_tokens[b:b+after]]
        matches = [i for i in range(len(tokens) - len(expected) + 1)
                   if tokens[i:i + len(expected)] == expected]
        if context['id'] not in full_return_ranges:
            verified = _verified_full_return(record, context, tokens)
            full_return_ranges[context['id']] = None if verified is None else verified[0]
        ranges = full_return_ranges[context['id']]
        if ranges is not None:
            matches = [i for i in matches if (i + before, i + before + len(words)) == ranges.get(order)]
            if not matches:
                raise ValueError(f'Segment {order}, {value}. The saved full return does not contain this '
                                 'value at the selected occurrence with the requested context.')
        if len(matches) != 1:
            raise ValueError(f'Segment {order}, {value}. The saved return needs one match for the '
                             f'replacement and its neighboring words, but found {len(matches)}.')
        start = matches[0]
        owned = times[start + before:start + before + len(words)]
        if any(lo == hi for lo, hi in owned):
            raise ValueError('The replacement includes a word with no measurable duration.')
        alignment_hash = source_alignment_fingerprint(context)
        binding = dict(audio_sha256=context['sha256'], alignment_sha256=alignment_hash, slot_order=order)
        native = bool(context.get('native_context_take'))
        review_cut = partial and preserve_base
        may_cut_full = review_cut and not native and ranges is not None and context.get('kind') == 'take'
        verified_native = (native and verify_native_cut is not None and
                        context['generation'].get('mode') == 'native_context' and
                        context['generation'].get('slot_order') == order and
                        [index for index, word in enumerate(context['words'])
                         if word.get('segment_order') == order] == list(range(start, start + len(owned))))
        native_scope = review_cut and verified_native

        def native_region():
            if context['id'] not in native_cut_regions:
                native_cut_regions[context['id']] = verify_native_cut(context, pcm)
            return native_cut_regions[context['id']]

        if detail.get('phoneme_alignment'):
            from phoneme_snap import propose

            if 'manual_return_cut' in variant or not (verified_native or ranges is not None):
                raise ValueError('Phoneme snapping needs a verified full return and automatic cuts.')
            char_start, char_end = marker_bounds[order]
            replacement = value + group['slot'].get('literal_suffix', '')
            target = transcript[:char_start] + replacement + transcript[char_end:]
            if _tokens(target) != tokens:
                raise ValueError('The saved return differs from the single replacement transcript.')
            phone_evidence = alignment.evidence(context, target)
            generated = native_region() if verified_native else (0, len(pcm))
            proposal = propose(source, context, source_phones, phone_evidence,
                               {'source_char_start': char_start, 'source_char_end': char_end},
                               replacement, generated, source_pcm=source_pcm, context_pcm=pcm)
            if proposal['source_region'] != group['source_region']:
                raise ValueError('The replacement does not fit the shared phoneme cut.')
            region = [sample + generated[0] for sample in proposal['insert_region']]
            candidate = dict(id=variant['id'], value=value, context_asset_id=context['id'],
                             returned_region=region, word_bounds=region)
            receipt = dict(choice_id=variant['id'], audio_sha256=context['sha256'],
                           alignment_sha256=alignment_hash, outer_joins=[],
                           phoneme_joins=proposal['phoneme_joins'])
            return candidate, receipt

        return_cut = variant.get('manual_return_cut')
        return_cuts = {}
        audio_bounds = None
        if 'manual_return_cut' in variant:
            return_cuts = _manual_cuts(return_cut, allowed=may_cut_full or native_scope, binding=binding,
                invalid_message='Choose a manual cut for a held edge of a verified replacement recording.',
                stale_message='The replacement recording, occurrence or word timings changed. Review the cut again.')
            if native_scope:
                audio_bounds = native_region()
                if not audio_bounds[0] <= owned[0][0] < owned[-1][1] <= audio_bounds[1]:
                    raise ValueError('The replacement word timings extend outside the saved generated Fish audio.')
        try:
            region, joins = _window(pcm, times, start, start + len(expected), threshold,
                                    recording='replacement', automatic=detail['automatic_context'],
                                    manual_cuts=return_cuts, audio_bounds=audio_bounds)
        except _JoinBoundaryError as error:
            if may_cut_full:
                error.full_return_timing_review_available = True
            if error.edge not in return_cuts and (may_cut_full or native_scope):
                lo = error.window['previous_end'] + 1
                hi = error.window['following_start'] - 1
                if native_scope and lo <= hi:
                    try:
                        first, last = native_region()
                    except ValueError:
                        raise error from None
                    if not first <= owned[0][0] < owned[-1][1] <= last:
                        raise error from None
                    lo, hi = max(lo, first), min(hi, last)
                    if lo <= hi:
                        error.manual_return_bounds = [lo, hi]
                if lo <= hi:
                    error.manual_return_cut = {**binding, 'edge': error.edge}
            raise
        bounds = [owned[0][0], owned[-1][1]]
        if (source_padding := detail.get('source_quiet_padding_samples')) is not None:
            _match_edge_padding(region, joins, pcm, times, start, start + len(expected), source_padding, threshold,
                                allow_contextual_cuts=preserve_base)
        if return_cut and native_scope:
            first, last = native_region()
            if not first <= region[0] < region[1] <= last:
                raise ValueError('The replacement cut must stay within the saved generated Fish audio.')
        if detail['automatic_context'] and not preserve_base:
            for side, seam in _quiet_handles(pcm, region, bounds, before, after, threshold):
                if group['returned_region'][side] == group['word_bounds'][side]:
                    region[side] = bounds[side]
                    joins[side] = seam
        candidate = dict(id=variant['id'], value=value, context_asset_id=context['id'],
                         returned_region=region, word_bounds=region if preserve_base else bounds)
        base_cut = detail.get('manual_base_cut')
        base_cuts = base_cut if isinstance(base_cut, list) else [base_cut] if base_cut else []
        connected = {'lead' if cut['edge'] == 'lead-in' else 'tail'
                     for cut in (*base_cuts, *return_cuts.values())}
        if connected:
            candidate['connected_join_sides'] = sorted(connected)
        if require_roles and _clip_roles(group['id'], group) != _clip_roles(group['id'], candidate):
            raise ValueError('The available window gives the source and return different clip roles.')
        neighbors = {}
        for first, spans in ((start, source_word_spans[a-before:a]),
                             (start + before + len(words), source_word_spans[b:b+after])):
            for index, span in enumerate(spans, first):
                neighbors[str(index)] = [span['source_start'], span['source_end']]
        if neighbors:
            candidate['context_word_links'] = dict(
                audio_sha256=context['sha256'], alignment_sha256=alignment_hash, spans=neighbors)
        receipt = dict(choice_id=variant['id'], audio_sha256=context['sha256'],
                       alignment_sha256=alignment_hash, outer_joins=joins)
        if ranges is not None:
            receipt['full_transcript_word_range'] = list(ranges[order])
        if return_cut:
            receipt['manual_return_cut'] = return_cut
        return candidate, receipt

    choices_by_order, audited, settings, fallback_only = [], [], [], []
    for spec in specs:
        order = spec.get('slot_order')
        if type(order) is not int or order not in by_order or order not in requested:
            raise ValueError('Choose a spoken delimiter occurrence from the current dialogue.')
        before, after = spec.get('words_before', 0), spec.get('words_after', 0)
        if any(type(n) is not int or not 0 <= n <= 48 for n in (before, after)):
            raise ValueError('Keep 0–48 matching words on each side of the delimiter.')
        threshold = spec.get('min_dbfs', -45)
        if type(threshold) not in (int, float) or not -75 <= threshold <= -30:
            raise ValueError('Choose a quiet-join threshold between −75 and −30 dBFS.')
        auto = spec.get('auto_context', False)
        if type(auto) is not bool:
            raise ValueError('Choose automatic or manual context.')
        match_padding = spec.get('match_padding', False)
        if type(match_padding) is not bool:
            raise ValueError('Choose whether to match quiet padding.')
        search_words = spec.get('auto_context_words', DEFAULT_CONTEXT_WORDS)
        if type(search_words) is not int or not 0 <= search_words <= 6:
            raise ValueError('Automatic context can check up to six words on each side.')
        if preserve_base:
            before = after = search_words = 0
            auto = True
        settings.append(dict(slot_order=order, auto_context=auto, auto_context_words=search_words,
                             words_before=before, words_after=after, min_dbfs=threshold,
                             match_padding=match_padding))
        slot = by_order[order]
        literal_silence = preserve_base and _literal_marker(slot)
        variants = spec.get('alternatives', [])
        a, b = all_intervals.get(order, (0, 0))
        may_cut_base = (partial and literal_silence and variants and order not in unusable
                        and source['generation'].get('adapter_input_text') == adapter_input
                        and a - 1 not in occupied and b not in occupied)
        manual_cut = spec.get('manual_base_cut')
        manual_cuts = {}
        if 'manual_base_cut' in spec:
            manual_cuts = _manual_cuts(manual_cut, allowed=may_cut_base, binding=source_binding,
                invalid_message='Choose a manual cut for a held literal base edge with a saved replacement.',
                stale_message='The base recording or its word timings changed. Review the cut again.')
            # A manually chosen boundary is only useful with a checked return.
            # Never use it to synthesize silence or discover a fallback.
            literal_silence = False
        if len(variants) > MAX_SEGMENTS or not variants and not (partial and literal_silence):
            raise ValueError('Choose at least one saved timed return for this occurrence.')
        if not variants:
            settings[-1]['fallback_only'] = True
        windows = ((lead, tail) for lead in range(min(a, search_words) + 1)
                   for tail in range(min(len(source_times) - b, search_words) + 1)) if auto else [(before, after)]
        options, reasons = [], []
        if order in unusable:
            if not partial:
                raise ValueError(f'Segment {order}, full dialogue take. {unusable[order]}')
            reasons.append({'reason': unusable[order], 'reason_code': 'unusable_baseline_occurrence'})
            windows = ()
        elif source_phones is not None and variants and not manual_cuts:
            try:
                options.append(phoneme_option(order))
            except ValueError as error:
                reasons.append(_hold_reason(error))
        for lead, tail in windows:
            try:
                packet, detail = propose_source(order, lead, tail, threshold, auto, manual_cuts)
            except ValueError as error:
                if not auto and not partial:
                    raise
                reason = _hold_reason(error)
                if (may_cut_base and isinstance(error, _JoinBoundaryError)
                        and error.edge not in manual_cuts):
                    gap = error.window['following_start'] - error.window['previous_end']
                    if gap > 1:
                        reason['manual_base_cut'] = {**source_binding, 'edge': error.edge}
                reasons.append(reason)
                continue
            detail['automatic_context'] = auto
            if manual_cut:
                detail['manual_base_cut'] = manual_cut
            if literal_silence:
                detail['literal_silence_eligible'] = True
            if match_padding:
                detail['source_quiet_padding_samples'] = _source_padding(
                    source_pcm, packet['source_region'], source_times, a-lead, b+tail, threshold)
            context_cost = 3 * (lead + tail)
            options.append((context_cost, packet, detail))
            if auto and not preserve_base:
                handles = list(_quiet_handles(source_pcm, packet['source_region'],
                                              packet['word_bounds'], lead, tail, threshold))
                # Keep the unchanged window as the first choice. Try alternate
                # roles only where the original handle has no measured speech.
                for bits in range(1, 1 << len(handles)):
                    region = list(packet['source_region'])
                    seams = list(detail['source_outer_joins'])
                    for bit, (side, seam) in enumerate(handles):
                        if bits & (1 << bit):
                            region[side] = seam['cut_sample']
                            seams[side] = seam
                    revised = {**detail, 'source_outer_joins': seams, 'returns': []}
                    if match_padding:
                        revised['source_quiet_padding_samples'] = _source_padding(
                            source_pcm, region, source_times, a-lead, b+tail, threshold)
                    options.append((context_cost + bits.bit_count(),
                                    {**packet, 'source_region': region, 'returned_region': region,
                                     'alternatives': []},
                                    revised))
        if not variants:
            reason = reasons[0] if not options else {
                'reason': 'The verified cut overlaps another delimiter in this preview.'}
            fallback_only.append({'slot_order': order, 'source_only': True, **reason})
        seen_ids = set()
        for variant in variants:
            if not options and not partial:
                break
            context = assets[variant['context_asset_id']]
            if context['id'] == source['id']:
                raise ValueError('Choose a different saved recording for the replacement.')
            ident, value = variant.get('id'), variant.get('value')
            if (not isinstance(ident, str) or not ident.strip()
                    or ident in ('original', SILENCE_FALLBACK, SAVED_FALLBACK)
                    or ident in seen_ids):
                raise ValueError('Each replacement needs a distinct recording choice.')
            seen_ids.add(ident)
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Enter the spoken value in this saved return.')
            receipt = dict(slot_order=order, choice_id=ident, value=value, recording_id=context['id'])
            audited.append(receipt)
            if context.get('native_context_take'):
                eligible = _recorded_values(context, context.get('generation') or {}, by_order)
                if (context.get('source_take_asset_id') != source['id'] or
                        eligible.get(order) != value):
                    message = 'Use this Fish context with its original dialogue take and replacement occurrence.'
                    if not partial:
                        raise ValueError(message)
                    receipt['reason'] = message
                    continue
            words = _tokens(value + slot.get('literal_suffix', ''))
            if not words or words == source_tokens[a:b]:
                message = 'Choose a spoken replacement different from the baseline.'
                if not partial:
                    raise ValueError(message)
                receipt['reason'] = message
                continue
            if not options:
                receipt.update(reasons[0])
                continue
            try:
                context = alignment.with_missing_clock(context)
                tokens, times = _timed_words(context)
            except ValueError as error:
                if not partial:
                    raise
                receipt['reason'] = str(error)
                continue
            pcm = read_pcm(context)
            compatible, return_reasons = [], []
            for cost, packet, detail in options:
                try:
                    candidate, evidence = propose_return(packet, detail, variant, context, words, tokens,
                                                          times, pcm, threshold, require_roles=auto or partial)
                except PhonemeJoinRejected as error:
                    if not partial:
                        raise
                    # Phone evidence is checked first. A measured contradiction
                    # holds this return; quieter waveform seams cannot repair it.
                    return_reasons.append(_hold_reason(error, prefix=f'Segment {order}, {value}. '))
                    break
                except ValueError as error:
                    if not auto and not partial:
                        raise
                    return_reasons.append(_hold_reason(error, prefix=f'Segment {order}, {value}. '))
                else:
                    packet['alternatives'].append(candidate)
                    detail['returns'].append(evidence)
                    compatible.append((cost, packet, detail))
            del pcm
            if not compatible:
                receipt.update(return_reasons[0])
                if manual_cut:
                    # The base cuts passed; this return has not joined yet.
                    receipt['pending_manual_base_cut'] = manual_cut
            if not partial:
                reasons.extend(return_reasons)
                options = compatible
        default = (fallback_values or {}).get(slot['type'])
        if find_fallback and default and literal_silence and options:
            # Preserve-base planning has a single delimiter-only source window.
            _, packet, detail = next((option for option in options if option[1]['alternatives']), options[0])
            alternatives = packet['alternatives']
            explicit = any(take['id'] == spec.get('initial_choice_id') or take['value'] == default
                           for take in alternatives)
            if not explicit:
                excluded = {variant['context_asset_id'] for variant in variants}
                checked = {}
                for context in find_fallback(slot, default, excluded):
                    variant = {'id': SAVED_FALLBACK, 'value': default, 'context_asset_id': context['id']}
                    try:
                        tokens, times = _timed_words(context)
                        candidate, receipt = propose_return(packet, detail, variant, context, _tokens(default),
                            tokens, times, read_pcm(context), threshold, require_roles=True)
                    except (ValueError, OSError):
                        continue
                    identity = (context['sha256'], *candidate['returned_region'])
                    checked.setdefault(identity, (context, candidate, receipt))
                    if len(checked) > 1:
                        detail['saved_fallback_ambiguous'] = True
                        break
                if len(checked) == 1:
                    context, candidate, receipt = next(iter(checked.values()))
                    assets[context['id']] = context
                    packet['saved_fallback'] = candidate
                    detail['saved_fallback'] = receipt
        if partial and not literal_silence:
            options = [option for option in options if option[1]['alternatives']]
        elif not partial and not options and preserve_base:
            raise ValueError(f"Segment {order}: no verified delimiter-only join. {reasons[-1]['reason']}")
        elif not partial and not options:
            raise ValueError(f'Segment {order}: no safe automatic context within {search_words} neighboring words '
                             f"on each side. {reasons[-1]['reason']}")
        choices_by_order.append((a, options))
    packets, evidence = _choose_packet_windows(choices_by_order, partial=partial)
    applied_cuts = {item['slot_order']: item['manual_base_cut'] for item in evidence
                    if 'manual_base_cut' in item}
    for setting in settings:
        if setting['slot_order'] in applied_cuts:
            setting['manual_base_cut'] = applied_cuts[setting['slot_order']]
    included = {(item['slot_order'], returned['choice_id']) for item in evidence for returned in item['returns']}
    held = [{**item, 'reason': item.get('reason',
             'Needs a different context window from the other replacements in this preview.')}
            for item in audited if (item['slot_order'], item['choice_id']) not in included]
    placed = {item['slot_order'] for item in evidence}
    held.extend(item for item in fallback_only if item['slot_order'] not in placed)
    return packets, dict(source_alignment_sha256=alignment_sha256,
                         status='audition_only', packets=evidence, settings=settings,
                         **({'derived_word_timings': alignment.derived} if alignment.derived else {}),
                         **({'literal_marker_labels': list(literal_labels.values())} if literal_labels else {}),
                         **({'held': held} if held else {}))
