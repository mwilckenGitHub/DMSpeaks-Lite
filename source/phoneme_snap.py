"""Choose speech joins from two independently aligned pronunciations."""

import unicodedata

from guide_bindings import RATE
from phoneme_alignment import UNKNOWN_PHONES, lexical_text, timeline_words, validate
from phonetic_join_profiles import join_rule

_ACOUSTIC_WINDOW = round(.035 * RATE)
_CONNECTED_GAP = round(.025 * RATE)
_TRANSITION_WINDOWS_MS = (10, 20, 30)
_TRANSITION_EXCESS_DB = 12
_STRONG_TRANSITION_EXCESS_DB = 14
_TRANSITION_FLOOR_DBFS = -45
_TERMINAL_FRAME = RATE // 200
_TERMINAL_HANDLE = round(.010 * RATE)


class PhonemeJoinRejected(ValueError):
    """Measured evidence contradicts a join; a weaker fallback must not hide it."""


def terminal_quiet_run(samples, final, *, release_allowance=round(.22 * RATE),
                       language='en-US', phone_set='ARPAbet'):
    """Find sustained terminal quiet near the measured release of the final phone."""
    import numpy as np

    release = final['end_sample']
    begin = max(final['start_sample'], release - round(.045 * RATE))
    count = (len(samples) - begin) // _TERMINAL_FRAME
    if count < 4:
        return None
    frames = np.asarray(samples[begin:begin + count * _TERMINAL_FRAME], dtype=np.float64)
    frames = frames.reshape(-1, _TERMINAL_FRAME)
    limit = 10 ** (-45 / 20)
    quiet_energy = np.mean(frames * frames, axis=1) <= limit ** 2
    quiet_peaks = np.max(np.abs(frames), axis=1) <= limit * 4
    quiet = quiet_energy & quiet_peaks
    phone = final.get('phone')
    manner = join_rule(phone, None, language=language, phone_set=phone_set).get('outgoing_manner', '')
    fricative = manner in {'sibilant fricative', 'non-sibilant fricative'}
    if fricative:
        # A fricative can carry sustained high-band energy below the
        # broadband silence threshold, sometimes past the aligned release.
        window = np.hanning(_TERMINAL_FRAME)
        power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
        bins = np.fft.rfftfreq(_TERMINAL_FRAME, 1 / RATE)
        band = power[:, bins >= 2500].sum(axis=1)
        high = 2 * band / (_TERMINAL_FRAME * np.sum(window ** 2))
        sustained = (high >= 10 ** (-66 / 10)) & (band >= .3 * power.sum(axis=1))
        bursts = np.flatnonzero(np.diff(np.r_[False, sustained, False])).reshape(-1, 2)
        for first, last in bursts:
            if last - first >= 3:
                quiet[first:last + 1] = False
    edges = np.flatnonzero(np.diff(np.r_[False, quiet, False])).reshape(-1, 2)
    # A stop closure can be quiet before a release. Require quiet through
    # the last full frames of the recording, allowing a short encoder tail.
    for first, last in reversed(edges):
        start = begin + int(first) * _TERMINAL_FRAME
        stop = begin + int(last) * _TERMINAL_FRAME
        if len(samples) - stop > _TERMINAL_HANDLE:
            break
        if fricative:
            start = max(start, release)
        remainder = np.asarray(samples[stop:])
        if (stop - start >= 4 * _TERMINAL_FRAME and start <= release + release_allowance
                and np.max(np.abs(remainder), initial=0) <= limit * 4):
            return start, stop
    return None


def validate_pcm(pcm, asset):
    """Validate supplied mono samples before alignment or acoustic comparison."""
    import numpy as np

    if pcm is None:
        return
    samples = np.asarray(pcm)
    if (samples.ndim != 1 or samples.size != asset['sample_count']
            or not np.issubdtype(samples.dtype, np.number)
            or np.iscomplexobj(samples) or not np.isfinite(samples).all()):
        raise PhonemeJoinRejected('Join audio must contain finite mono samples matching the recording length.')


def _slot(words, text, start, end):
    selected = [index for index, word in enumerate(words)
                if word['char_start'] < end and word['char_end'] > start]
    if (not selected or words[selected[0]]['char_start'] < start
            or words[selected[-1]]['char_end'] > end
            or ''.join(lexical_text(words[index]['word']) for index in selected)
            != lexical_text(text[start:end])):
        raise ValueError('The delimiter must map to complete aligned words.')
    return selected[0], selected[-1] + 1


def _boundary_phone(word, *, last):
    phones = word['phonemes']
    if not phones:
        raise ValueError('A word at the join has no measured phonemes.')
    phone = phones[-1 if last else 0]
    if phone['phone'] in UNKNOWN_PHONES:
        raise ValueError('A word at the join has no resolved pronunciation.')
    return phone


def _label(phone):
    return unicodedata.normalize('NFC', phone['phone'])


def source_region(evidence, asset, transcript, start, end):
    words = validate(evidence, asset, transcript)
    left, right = _slot(words, transcript, start, end)
    return [_boundary_phone(words[left], last=False)['start_sample'],
            _boundary_phone(words[right - 1], last=True)['end_sample']]


def returned_name_region(evidence, asset, transcript, name, char_span):
    """Locate one explicitly bound name on the returned recording's sample clock."""
    start, end = char_span
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(transcript) or transcript[start:end] != name
            or not lexical_text(name)):
        raise ValueError('The requested name does not match its occurrence in the submitted transcript.')
    words = validate(evidence, asset, transcript)
    left, right = _slot(words, transcript, start, end)
    selected = words[left:right]
    if any(not word['phonemes'] or any(phone['phone'] in UNKNOWN_PHONES
                                      for phone in word['phonemes']) for word in selected):
        raise ValueError('The returned name has an unresolved pronunciation.')
    first = selected[0]['phonemes'][0]
    last = selected[-1]['phonemes'][-1]
    return {
        'sample_rate': asset['sample_rate'],
        'name_char_span': [start, end],
        'name_samples': [first['start_sample'], last['end_sample']],
        'previous_word_end_sample': words[left - 1]['end_sample'] if left else None,
        'next_word_start_sample': words[right]['start_sample'] if right < len(words) else None,
        'audio_sha256': evidence['audio_sha256'],
        'transcript_sha256': evidence['transcript_sha256'],
        'provider': evidence['provider'],
        'language': evidence['language'],
        'phone_set': evidence['phone_set'],
    }


def _edge_features(pcm, phone, at_lead):
    import numpy as np

    start, end = phone['start_sample'], phone['end_sample']
    if at_lead:
        start = max(start, end - _ACOUSTIC_WINDOW)
    else:
        end = min(end, start + _ACOUSTIC_WINDOW)
    signal = np.asarray(pcm[start:end], dtype=float)
    if signal.size < RATE // 100:
        return None
    db = 10 * np.log10(max(1e-12, float(np.mean(signal ** 2))))
    power = np.abs(np.fft.rfft(signal * np.hanning(signal.size), n=2048)) ** 2
    frequencies = np.fft.rfftfreq(2048, 1 / RATE)
    edges = (80, 200, 400, 700, 1100, 1700, 2600, 4000, 7000, 10000)
    bands = np.array([np.sum(power[(frequencies >= lo) & (frequencies < hi)])
                      for lo, hi in zip(edges, edges[1:])])
    return db, bands / max(1e-12, float(bands.sum()))


def _splice_transition(source_pcm, context_pcm, source_cut, return_cut,
                       source_span, return_span, *, at_lead):
    """Compare the hypothetical splice with both intact transitions."""
    import numpy as np

    measurements = []
    for milliseconds in _TRANSITION_WINDOWS_MS:
        count = round(milliseconds * RATE / 1000)
        if (not source_span[0] <= source_cut - count <= source_cut + count <= source_span[1]
                or not return_span[0] <= return_cut - count <= return_cut + count <= return_span[1]):
            break
        windows = (source_pcm[source_cut - count:source_cut], source_pcm[source_cut:source_cut + count],
                   context_pcm[return_cut - count:return_cut], context_pcm[return_cut:return_cut + count])
        levels = [10 * np.log10(max(1e-12, float(np.mean(np.square(window, dtype=float)))))
                  for window in windows]
        source_before, source_after, return_before, return_after = levels
        source_step, return_step = source_after - source_before, return_after - return_before
        splice_step = return_after - source_before if at_lead else source_after - return_before
        lower, upper = sorted((source_step, return_step))
        excess = max(0, lower - splice_step, splice_step - upper)
        measurements.append({
            'window_ms': milliseconds, 'source_step_db': round(source_step, 2),
            'return_step_db': round(return_step, 2), 'splice_step_db': round(splice_step, 2),
            'excess_db': round(excess, 2),
            'large_excess': bool(excess >= _TRANSITION_EXCESS_DB and min(levels) >= _TRANSITION_FLOOR_DBFS),
        })
    return measurements


def _join(source_fixed, return_fixed, outgoing, incoming, *, at_lead, source_pcm, context_pcm,
          language='', phone_set=''):
    source_phone = _boundary_phone(source_fixed, last=at_lead)
    return_phone = _boundary_phone(return_fixed, last=at_lead)
    if _label(source_phone) != _label(return_phone):
        raise PhonemeJoinRejected('The fixed word at the join has different aligned pronunciations.')
    fixed = 'end_sample' if at_lead else 'start_sample'
    variable = 'start_sample' if at_lead else 'end_sample'
    # A phone from the pronunciation of the adjacent fixed word can belong
    # to quiet stop closure. Keep its whole measured interval on its own side.
    if ((at_lead and source_phone[fixed] > outgoing[variable])
            or (not at_lead and source_phone[fixed] < outgoing[variable])
            or (at_lead and return_phone[fixed] > incoming[variable])
            or (not at_lead and return_phone[fixed] < incoming[variable])):
        raise PhonemeJoinRejected('Measured phonemes overlap across the delimiter.')
    source_pair = [_label(source_phone), _label(outgoing)]
    pair = [_label(return_phone), _label(incoming)]
    if not at_lead:
        source_pair.reverse()
        pair.reverse()
    source_pause = abs(outgoing[variable] - source_phone[fixed])
    return_pause = abs(incoming[variable] - return_phone[fixed])
    details = {'phones': pair, 'source_pause_samples': source_pause,
               'return_pause_samples': return_pause, 'acoustic_check': 'unavailable_audio'}
    if language and phone_set:
        details.update(
            source_phones=source_pair,
            source_rule=join_rule(*source_pair, language=language, phone_set=phone_set),
            return_rule=join_rule(*pair, language=language, phone_set=phone_set),
        )
    if source_pcm is None or context_pcm is None:
        return details
    # Compare two versions of the same fixed phone. Comparing the two
    # different phones across a join would penalize normal consonant onsets.
    source_edge = _edge_features(source_pcm, source_phone, at_lead)
    return_edge = _edge_features(context_pcm, return_phone, at_lead)
    details['acoustic_check'] = 'insufficient_window'
    if source_edge is not None and return_edge is not None:
        level = abs(source_edge[0] - return_edge[0])
        spectral = float(abs(source_edge[1] - return_edge[1]).sum()) / 2
        details.update(acoustic_check='compared',
                       fixed_phone_level_difference_db=round(float(level), 2),
                       fixed_phone_spectral_distance=round(spectral, 3))
        # An acoustic mismatch this large is enough to distrust even matching
        # labels at a connected join. These are conservative audition defaults.
        # Either recording may carry a connected transition. A pause in the
        # other recording must not exempt that transition from this check.
        if min(source_pause, return_pause) <= _CONNECTED_GAP and (
                level > 18 or spectral > .8 and min(source_edge[0], return_edge[0]) > -45):
            raise PhonemeJoinRejected('The fixed phoneme differs acoustically at a connected join.')
    if max(source_pause, return_pause) <= _CONNECTED_GAP:
        source_left, source_right = (source_phone, outgoing) if at_lead else (outgoing, source_phone)
        return_left, return_right = (return_phone, incoming) if at_lead else (incoming, return_phone)
        transition = _splice_transition(
            source_pcm, context_pcm, outgoing[variable], incoming[variable],
            (source_left['start_sample'], source_right['end_sample']),
            (return_left['start_sample'], return_right['end_sample']), at_lead=at_lead)
        details['splice_transition'] = transition
        # Ignore a single window at a pulse or stop release. An audible
        # excess can persist for 20 ms and soften in the widest window.
        sustained = (len(transition) == len(_TRANSITION_WINDOWS_MS)
                     and all(row['large_excess'] for row in transition))
        strong_short_step = len(transition) >= 2 and all(
            row['large_excess'] and row['excess_db'] >= _STRONG_TRANSITION_EXCESS_DB
            for row in transition[:2])
        if sustained or strong_short_step:
            side = 'leading' if at_lead else 'trailing'
            raise PhonemeJoinRejected(f'The {side} splice introduces a level jump absent from both recordings.')
    return details


def propose(source, context, source_evidence, context_evidence, binding, replacement, generated,
            *, source_pcm=None, context_pcm=None):
    """Keep the base cadence and copy only the measured replacement span."""
    validate_pcm(source_pcm, source)
    validate_pcm(context_pcm, context)
    if any(source_evidence.get(key) != context_evidence.get(key)
           for key in ('language', 'phone_set', 'provider')):
        raise ValueError('Source and replacement need compatible phoneme alignments.')
    transcript = source['generation']['spoken_text']
    start, end = binding['source_char_start'], binding['source_char_end']
    if not 0 <= start < end <= len(transcript):
        raise ValueError('The source delimiter span is invalid.')
    target = transcript[:start] + replacement + transcript[end:]
    base = validate(source_evidence, source, transcript)
    returned = validate(context_evidence, context, target)
    left, right = _slot(base, transcript, start, end)
    new_left, new_right = _slot(returned, target, start, start + len(replacement))
    if [word['word'] for word in base[:left] + base[right:]] != [
            word['word'] for word in returned[:new_left] + returned[new_right:]]:
        raise ValueError('The fixed transcript has a different word alignment.')
    insert = returned[new_left:new_right]
    if any(not word['phonemes'] or any(phone['phone'] in UNKNOWN_PHONES for phone in word['phonemes'])
           for word in insert):
        raise ValueError('The replacement has an unresolved pronunciation.')
    old_first, old_last = _boundary_phone(base[left], last=False), _boundary_phone(base[right - 1], last=True)
    new_first, new_last = _boundary_phone(insert[0], last=False), _boundary_phone(insert[-1], last=True)
    cut_in, cut_out = new_first['start_sample'], new_last['end_sample']
    begin, stop = generated
    if not 0 <= begin <= cut_in < cut_out <= stop <= context['sample_count']:
        raise ValueError('The measured replacement crosses the generated audio window.')

    lead, tail = old_first['start_sample'], old_last['end_sample']
    if lead >= tail:
        raise ValueError('The source phoneme cuts do not enclose the delimiter.')
    joins = {}
    profile = {'language': context_evidence['language'], 'phone_set': context_evidence['phone_set']}
    if left:
        joins['lead'] = _join(base[left - 1], returned[new_left - 1], old_first, new_first,
                              at_lead=True, source_pcm=source_pcm, context_pcm=context_pcm, **profile)
    else:
        joins['lead_rule'] = join_rule(None, _label(new_first), **profile)
    if right < len(base):
        joins['tail'] = _join(base[right], returned[new_right], old_last, new_last,
                              at_lead=False, source_pcm=source_pcm, context_pcm=context_pcm, **profile)
    elif source_pcm is not None and context_pcm is not None and stop == context['sample_count']:
        source_quiet = terminal_quiet_run(source_pcm, old_last, **profile)
        return_quiet = terminal_quiet_run(context_pcm, new_last, **profile)
        if source_quiet and return_quiet and (tail < source_quiet[0] or cut_out < return_quiet[0]):
            tail = max(tail, source_quiet[0])
            cut_out = max(cut_out, return_quiet[0])
            joins['tail'] = {'method': 'terminal_waveform_decay',
                             'source_phone_end_sample': old_last['end_sample'],
                             'return_phone_end_sample': new_last['end_sample'],
                             'source_waveform_quiet_samples': list(source_quiet),
                             'return_waveform_quiet_samples': list(return_quiet),
                             'source_cut_sample': tail,
                             'return_cut_sample': cut_out}
    if right == len(base):
        joins['tail_rule'] = join_rule(_label(new_last), None, **profile)
    return {
        'source_region': [lead, tail],
        'insert_region': [cut_in - begin, cut_out - begin],
        'excluded_return_samples': cut_in - begin + stop - cut_out,
        'base_words': timeline_words(source_evidence, base),
        'replacement_words': timeline_words(context_evidence, insert, origin=cut_in),
        'phoneme_joins': joins,
        'timing_basis': 'phoneme_alignment',
        'audition_only': True,
    }
