"""Propose native replacement cuts from matched words and measured quiet."""

import json

from layered_audio import RATE
from fish_community_tts import _normalize_timestamps
from speech_edit_authoring import WORDS
from timed_contextual_packets import quiet_edge_padding, quiet_seam


def _tokens(text):
    return [word.replace('’', "'").casefold() for word in WORDS.findall(text)]


def _words(evidence, asset, samples):
    if evidence.get('audio_sha256') != asset.get('sha256'):
        raise ValueError('Word evidence belongs to a different recording.')
    tokens, spans = [], []
    prior_start = prior_end = 0
    for word in evidence['words']:
        if not isinstance(word, dict) or not isinstance(word.get('word'), str):
            raise ValueError('Each timing label must describe one spoken word.')
        labels = _tokens(word['word'])
        if len(labels) != 1:
            raise ValueError('Each timing label must describe one spoken word.')
        start, end = word.get('start'), word.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not 0 <= start <= end <= len(samples) / RATE):
            raise ValueError('Word evidence lies outside the recording.')
        span = round(start * RATE), round(end * RATE)
        if span[0] < prior_start or span[1] < prior_end:
            raise ValueError('Word evidence is out of order.')
        tokens.extend(labels)
        spans.append(span)
        prior_start, prior_end = span
    return tokens, spans


def _locate(tokens, before, value, after):
    expected = before + value + after
    matches = [i + len(before) for i in range(len(tokens) - len(expected) + 1)
               if tokens[i:i + len(expected)] == expected]
    if len(matches) != 1:
        raise ValueError('The replacement and neighboring words do not identify a unique join.')
    return matches[0], matches[0] + len(value)


def _quiet_cut(pcm, start, end, threshold, *, towards_end=False):
    _, measured = quiet_seam(pcm, start, end, threshold, require_unique=True)
    lo, hi = measured['quiet_span']
    guard = min(RATE // 50, (hi - lo) // 2)
    center = hi - guard if towards_end else lo + guard
    radius = RATE * 3 // 1000
    cut = min(range(center - radius, center + radius + 1),
              key=lambda index: (abs(pcm[index]), abs(index - center)))
    return cut, measured['quiet_span']


def _returned_lead_cut(pcm, begin, preceding, value_start, threshold, *, source_clock):
    """Find a safe lead cut when the return contains earlier speech."""
    preceding_start, preceding_end = preceding
    needs_cut = preceding_start >= begin or preceding_end > begin + RATE // 10
    if not source_clock and preceding_end > begin:
        try:
            return _quiet_cut(pcm, preceding_end, value_start, min(-55, threshold),
                              towards_end=True)
        except ValueError:
            if needs_cut:
                raise ValueError('The return contains preceding speech with no verified lead-in cut.') from None
    if needs_cut:
        raise ValueError('The generated start may contain a preceding word.')
    return begin, None


def provider_evidence(source, context, binding, replacement, generated, header):
    """Map provider labels to the actual generated span inside the codec decode."""
    try:
        labels, warning = _normalize_timestamps(json.loads(header))
    except (TypeError, ValueError):
        raise ValueError('The native return has no usable provider word times.') from None
    if warning or not labels:
        raise ValueError('The native return has no usable provider word times.')
    transcript = source['generation']['spoken_text']
    start, stop = binding['source_char_start'], binding['source_char_end']
    prefix, insert, suffix = map(_tokens, (transcript[:start], replacement, transcript[stop:]))
    if [_tokens(word['word']) for word in labels] != [[word] for word in prefix + insert + suffix]:
        raise ValueError('Provider labels differ from the requested speech.')
    index = len(prefix)
    span_seconds = (generated[1] - generated[0]) / RATE
    if (not insert or any(word['end'] - word['start'] > 2048 / RATE for word in labels[:index])
            or labels[index]['start'] > 4096 / RATE
            or labels[index + len(insert) - 1]['end'] > span_seconds
            or labels[-1]['end'] > span_seconds + .001):
        raise ValueError('Provider timing leaves uncertain speech at the native edit edges.')
    offset = generated[0] / RATE
    adjusted = [{**word, 'start': offset + min(word['start'], span_seconds),
                 'end': offset + min(word['end'], span_seconds)} for word in labels[index:]]
    return {
        'source_evidence': {'audio_sha256': source['sha256'], 'words': source.get('words', [])},
        'context_evidence': {'audio_sha256': context['sha256'],
                             'words': [*source.get('words', [])[:index], *adjusted],
                             'prefix_from_source': True},
    }


def _isolated_word_edges(insert_evidence, pcm, generated, expected, threshold):
    """Use measured edges when isolated recognition corroborates one whole word."""
    if insert_evidence is None or len(expected) != 1:
        return None
    begin, end = generated
    insert = pcm[begin:end]
    evidence, asset = insert_evidence
    labels, spans = _words(evidence, asset, insert)
    if labels != expected or spans[0][0] >= spans[0][1]:
        return None
    level = min(-55, threshold)
    center = (begin + end) // 2
    lead = quiet_edge_padding(pcm, begin, center, level)
    tail = quiet_edge_padding(pcm, end, center, level)
    if min(lead, tail) < 3 * (RATE // 100) or end - begin - lead - tail < RATE // 20:
        return None
    if max(map(abs, pcm[begin+lead:end-tail])) < .01:
        return None
    return begin + lead, end - tail


def propose(source, source_pcm, source_evidence, context, context_pcm, context_evidence,
            binding, replacement, generated, *, recognized_as=None, min_dbfs=-45,
            insert_evidence=None):
    """Return audition coordinates. The timings and cuts do not approve the speech."""
    if not -75 <= min_dbfs <= -30:
        raise ValueError('Choose a quiet-join threshold between −75 and −30 dBFS.')
    for media, pcm in ((source, source_pcm), (context, context_pcm)):
        if (media.get('sample_rate') != RATE or media.get('channels') != 1
                or media.get('sample_count') != len(pcm)):
            raise ValueError('Join review needs matching mono 44.1 kHz recordings.')
    begin, end = generated
    if not (0 <= begin < end <= len(context_pcm)):
        raise ValueError('The generated span lies outside its decoded context.')

    transcript = source['generation']['spoken_text']
    start, stop = binding['source_char_start'], binding['source_char_end']
    if not 0 <= start < stop <= len(transcript):
        raise ValueError('The source needs a spoken delimiter value.')
    before, after = _tokens(transcript[:start])[-3:], _tokens(transcript[stop:])[:3]
    original = _tokens(transcript[start:stop])
    expected = _tokens(recognized_as if recognized_as is not None else replacement)
    if not original or not expected or len(expected) != len(_tokens(replacement)):
        raise ValueError('The replacement needs independently identifiable spoken words.')

    source_words, source_times = _words(source_evidence, source, source_pcm)
    context_words, context_times = _words(context_evidence, context, context_pcm)
    left, right = _locate(source_words, before, original, after)
    new_left, new_right = _locate(context_words, before, expected, after)
    name_start, name_end = context_times[new_left][0], context_times[new_right - 1][1]
    acoustic_edges = None
    if (before and after and not context_evidence.get('prefix_from_source')
            and 0 < begin - name_start <= RATE // 4 and begin < name_end <= end
            and context_times[new_left - 1][1] <= begin and context_times[new_right][0] >= end):
        acoustic_edges = _isolated_word_edges(insert_evidence, context_pcm, generated, expected, min_dbfs)
        if acoustic_edges is not None:
            name_start, name_end = acoustic_edges
    if not begin <= name_start < name_end <= end:
        raise ValueError('The recognized replacement is outside the generated span.')

    insert_start, returned_lead = begin, None
    if before:
        insert_start, returned_lead = _returned_lead_cut(
            context_pcm, begin, context_times[new_left - 1], name_start, min_dbfs,
            source_clock=context_evidence.get('prefix_from_source') is True)
        lead, lead_quiet = _quiet_cut(source_pcm, source_times[left - 1][1],
                                      source_times[left][0], min_dbfs, towards_end=True)
    elif left == new_left == 0:
        lead, lead_quiet = 0, None
    else:
        raise ValueError('The beginning of the replacement is ambiguous.')

    if after:
        tail, tail_quiet = _quiet_cut(source_pcm, source_times[right - 1][1],
                                      source_times[right][0], min_dbfs)
        # The next word may begin ahead of its estimated label. A stricter
        # floor keeps residual speech from masquerading as a second pause.
        if acoustic_edges is not None:
            cutoff, returned_quiet = end, [name_end, end]
        else:
            cutoff, returned_quiet = _quiet_cut(context_pcm, name_end, context_times[new_right][0],
                                                 min(-55, min_dbfs))
            # If the insert ends before the detected continuation, keep it whole.
            cutoff = min(cutoff, end)
    elif right == len(source_words) and new_right == len(context_words):
        tail, tail_quiet, cutoff, returned_quiet = len(source_pcm), None, end, None
    else:
        raise ValueError('The end of the replacement is ambiguous.')
    if lead >= tail or not begin <= insert_start < cutoff <= end:
        raise ValueError('The resulting cuts do not leave a complete replacement.')

    quiet_intervals = {'source_lead': lead_quiet, 'source_tail': tail_quiet,
                       'returned_tail': returned_quiet}
    if returned_lead is not None:
        quiet_intervals['returned_lead'] = returned_lead
    word_spans = [acoustic_edges] if acoustic_edges is not None else context_times[new_left:new_right]
    pause_match = {}
    if acoustic_edges is not None:
        # Preserve the surrounding fixed speech. Only discard excess quiet in
        # the isolated return, using the same measurement as boundary review.
        from native_pause_match import THRESHOLD_DBFS, measure
        try:
            (insert_start, cutoff), details = measure(
                source_pcm, [lead, tail], context_pcm, [insert_start, cutoff], [True, True],
                threshold=min(THRESHOLD_DBFS, min_dbfs))
            pause_match = {'pause_match': details}
            lead_padding, tail_padding = details['return_padding_samples']
            name_start, name_end = begin + lead_padding, end - tail_padding
        except ValueError:
            pass  # The whole return remains available when there is no excess quiet.
        # Keep the estimated label within the retained return. Frames removed
        # by the pause measurement contained no speech at its selected floor.
        word_spans = [(max(insert_start, name_start), min(cutoff, name_end))]
        quiet_intervals['returned_lead'] = [begin, name_start]
    replacement_words = []
    for label, (word_start, word_end) in zip(WORDS.findall(replacement), word_spans):
        if not insert_start <= word_start <= word_end <= cutoff:
            raise ValueError('The proposed cut would remove part of a timed replacement word.')
        if word_end > word_start:
            replacement_words.append({'word': label, 'start': (word_start - insert_start) / RATE,
                                      'end': (word_end - insert_start) / RATE})
    return {
        'source_region': [lead, tail],
        'insert_region': [insert_start - begin, cutoff - begin],
        'quiet_intervals': quiet_intervals,
        'excluded_return_samples': insert_start - begin + end - cutoff,
        'recognized_as': recognized_as,
        'replacement_words': replacement_words,
        **pause_match,
        **({'word_clock_basis': 'isolated_word_activity'} if acoustic_edges is not None else {}),
        'audition_only': True,
    }
