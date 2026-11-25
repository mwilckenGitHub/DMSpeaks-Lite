"""Measured phoneme boundaries from the DMSpeaks dialogue assembler."""
from bisect import bisect_right
import re
import numpy as np
from phoneme_snap import PhonemeJoinRejected, _ACOUSTIC_WINDOW, _TRANSITION_WINDOWS_MS, _join, terminal_quiet_run
from phoneme_alignment import UNKNOWN_PHONES, lexical_text
from phonetic_join_profiles import join_rule
from quiet_boundaries import ALIGNMENT_TOLERANCE, FRAME, HANDLE, quiet_runs
from guide_bindings import RATE

QUIET_CLOCK_MARGIN = round(.005 * RATE)


STOP_CLOSURE_MINIMUM = round(.045 * RATE)


VOICED_FRICATIVES = frozenset({'V', 'DH', 'Z', 'ZH'})


VOICELESS_STOPS = frozenset({'P', 'T', 'K'})


NUMBER_WORDS = ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
                'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen',
                'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen')


TENS = ('', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety')


ORDINAL_ENDINGS = {
    'one': 'first', 'two': 'second', 'three': 'third', 'five': 'fifth',
    'eight': 'eighth', 'nine': 'ninth', 'twelve': 'twelfth',
}


def integer_words(value):
    if value < 20:
        return NUMBER_WORDS[value]
    if value < 100:
        tens, units = divmod(value, 10)
        return TENS[tens] + (' ' + NUMBER_WORDS[units] if units else '')
    scale, label = (1000, 'thousand') if value >= 1000 else (100, 'hundred')
    quotient, remainder = divmod(value, scale)
    return integer_words(quotient) + ' ' + label + (' ' + integer_words(remainder) if remainder else '')


def expanded(text):
    def spell(match):
        digits = match[1].replace(',', '')
        suffix = match[2]
        if len(digits) > 1 and (digits.startswith('0') or len(digits) > 6):
            spoken = ' '.join(NUMBER_WORDS[int(digit)] for digit in digits)
        else:
            spoken = integer_words(int(digits))
        if suffix is None:
            return spoken
        prefix, space, last = spoken.rpartition(' ')
        ending = ORDINAL_ENDINGS.get(last)
        if ending is None:
            ending = last[:-1] + 'ieth' if last.endswith('y') else last + 'th'
        return prefix + space + ending
    return re.sub(r'\b([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(st|nd|rd|th)?\b', spell, text, flags=re.I)


def quiet_boundary(samples, aligned, index):
    """Locate the nearest measured quiet interval around an interior word boundary."""
    left, right = aligned[index - 1], aligned[index]
    left_edge = left['phonemes'][-1] if left['phonemes'] else left
    right_edge = right['phonemes'][0] if right['phonemes'] else right
    release = left_edge['end_sample']
    onset = right_edge['start_sample']
    # The phone clock usually assigns silence to a neighboring sound. Keep the
    # search local to the shared boundary and require measured quiet.
    lower = max(left_edge['start_sample'], release - round(.045 * RATE))
    upper = min(right_edge['end_sample'], onset + round(.045 * RATE))
    gaps = quiet_runs(samples, lower, upper, minimum_frames=4)
    if not gaps:
        return None
    clock = (release + onset) // 2
    start, end = min(gaps, key=lambda gap: abs((gap[0] + gap[1]) // 2 - clock))
    return start, end, clock


def quiet_cut(gap, side):
    start, end = gap[:2]
    point = end - HANDLE if side == 'before' else start + HANDLE
    return min(end - 1, max(start + 1, point))


def adjacent_phone_edges(aligned, index):
    """Report the measured release and onset surrounding a proposed cut."""
    return {
        'release_sample': aligned[index - 1]['phonemes'][-1]['end_sample']
        if index > 0 and aligned[index - 1]['phonemes'] else None,
        'onset_sample': aligned[index]['phonemes'][0]['start_sample']
        if index < len(aligned) and aligned[index]['phonemes'] else None,
    }


def adjacent_donor_cut(samples, aligned, index, observation, *, leading):
    """Confirm a quiet cut between two independently recognized delimiter words."""
    observed = observation.get('words', ())
    if not (0 < index <= len(aligned) == len(observed)) or any(
            lexical_text(expanded(word['word'])) != lexical_text(expanded(heard['word']))
            for word, heard in zip(aligned, observed, strict=True)):
        return None
    edges = adjacent_phone_edges(aligned, index)
    release, onset = edges['release_sample'], edges['onset_sample']
    if index == len(aligned) and not leading:
        # This take ends at the retained delimiter and has no following word.
        final = aligned[-1]['phonemes'][-1]
        gap = terminal_quiet_run(samples, final) or recognized_terminal_quiet(samples, final, observation)
        if gap:
            return quiet_cut((*gap, release), 'after'), gap
        return None
    if release is None or onset is None or not 0 <= release <= onset <= len(samples):
        return None
    heard_release = round(observed[index - 1]['end'] * RATE)
    heard_onset = round(observed[index]['start'] * RATE)
    if release < onset:
        # Trim within the measured pause. The retained word's boundary must
        # agree on both clocks even when the unretained edge is less precise.
        actual, heard = (onset, heard_onset) if leading else (release, heard_release)
        if abs(actual - heard) > ALIGNMENT_TOLERANCE or heard_release > heard_onset:
            return None
        runs = quiet_runs(samples, release, onset, minimum_frames=4)
        if not runs:
            return None
        gap = runs[-1] if leading else runs[0]
        return quiet_cut((*gap, actual), 'before' if leading else 'after'), gap
    if (not FRAME <= release <= len(samples) - FRAME
            or any(abs(clock - release) > ALIGNMENT_TOLERANCE
                   for clock in (heard_release, heard_onset))):
        return None
    gap = release - FRAME, release + FRAME
    if quiet_runs(samples, *gap, minimum_frames=2) == [gap]:
        return release, gap
    return None


def sample_cut(samples, aligned, index, side):
    """Find quiet near the phonetic release and onset of neighboring words."""
    if index == 0:
        return 0
    if index == len(aligned):
        return len(samples)
    gap = quiet_boundary(samples, aligned, index)
    return quiet_cut(gap, side) if gap else None


def recognized_terminal_decay(samples, final):
    """Confirm that a late terminal quiet run follows steadily falling energy."""
    gap = terminal_quiet_run(samples, final, release_allowance=RATE)
    if gap is None:
        return None
    tail = samples[final['end_sample']:gap[0]]
    frames = len(tail) // round(.06 * RATE)
    if frames < 4:
        return None
    energy = np.array([np.mean(block * block) for block in np.array_split(tail, frames)])
    if energy[-1] <= energy[0] / 5 and np.all(energy[1:] <= 2.5 * energy[:-1]):
        return gap
    return None


def recognized_terminal_quiet(samples, final, observation):
    """Allow a quiet release after matching word and phone clocks."""
    words = observation.get('words', ())
    if not words:
        return None
    word_start = round(words[-1]['start'] * RATE)
    word_end = round(words[-1]['end'] * RATE)
    if (not 0 <= word_start <= word_end <= len(samples)
            or abs(word_end - final['end_sample']) > round(.22 * RATE)):
        return None
    gap = terminal_quiet_run(samples, final, release_allowance=RATE)
    release = max(word_end, final['end_sample'])
    start = min(word_start, final['start_sample'])
    if gap is None or start >= release or not 0 < gap[0] - release <= round(.65 * RATE):
        return None
    final_word = samples[start:release]
    tail = samples[release:gap[0]]
    power_limit = min(10 ** (-30 / 10), np.mean(final_word ** 2) * 10 ** (-1.5 / 10))
    frame = round(.020 * RATE)
    if (recognized_terminal_decay(samples, final) is None
            and any(np.mean(tail[start:start + frame] ** 2) > power_limit
                    for start in range(0, len(tail), frame))):
        return None
    # Reject returning periodic energy. A separate voiced sound can have
    # relatively low amplitude and still persist into a measured quiet run.
    frame = round(.030 * RATE)
    for start in range(0, len(tail) - frame + 1, frame):
        signal = tail[start:start + frame:3].astype(np.float64)
        signal -= signal.mean()
        if np.mean(signal ** 2) <= 10 ** (-40 / 10):
            continue
        for lag in range(RATE // (3 * 350), RATE // (3 * 75) + 1):
            after, before = signal[lag:], signal[:-lag]
            if after @ before > .8 * np.sqrt((after @ after) * (before @ before)):
                return None
    return gap


def phone_candidates(samples, aligned, begin, end, slot_begin, slot_end):
    starts = {index: sample_cut(samples, aligned, index, 'before')
              for index in range(begin, slot_begin + 1)}
    ends = {index: sample_cut(samples, aligned, index, 'after')
            for index in range(slot_end, end + 1)}
    return starts, ends


def voiced_stop_closure(samples, words, index, source_words, source_index):
    """Find the quiet closure before a voiced stop following a released voiceless stop."""
    if not (0 < index < len(words) and 0 < source_index < len(source_words)):
        return None
    left, right = words[index-1:index+1]
    original_left, original_right = source_words[source_index-1:source_index+1]
    if ([lexical_text(w['word']) for w in (left, right)]
            != [lexical_text(w['word']) for w in (original_left, original_right)]):
        return None
    if not all(w['phonemes'] for w in (left, right, original_left, original_right)):
        return None
    release, onset = left['phonemes'][-1], right['phonemes'][0]
    if (release['phone'] not in VOICELESS_STOPS or onset['phone'] not in {'B', 'D', 'G'}
            or original_left['phonemes'][-1]['phone'] != release['phone']
            or original_right['phonemes'][0]['phone'] != onset['phone']
            or release['end_sample'] != onset['start_sample']):
        return None
    start, end = onset['start_sample'], onset['end_sample']
    runs = quiet_runs(samples, start, end, minimum_frames=4)
    if len(runs) != 1:
        return None
    a, b = runs[0]
    if a-start > ALIGNMENT_TOLERANCE or b-a < STOP_CLOSURE_MINIMUM or end-b < FRAME:
        return None
    # Reject a new attack before the closure. Only the preceding release may decay here.
    length = (a-start)//FRAME
    if length >= 2:
        frames = samples[start:start+length*FRAME].reshape(-1,FRAME)
        levels = np.mean(frames**2,axis=1)
        if np.any(levels[1:] > np.maximum(10**(-45/10), 2*levels[:-1])):
            return None
    if np.max(np.abs(samples[b:end])) < .03:
        return None
    return a, b


def closure_in_phone(pcm, phone):
    """Find a low-energy closure bounded by a decay and an audible release."""
    frame, hop, side = 220, 88, 1764
    start, stop = phone["start_sample"], phone["end_sample"]
    if stop - start < 3 * frame or start < side or stop + side > len(pcm):
        return None
    offsets = np.arange(start - side, stop + side - frame + 1, hop)
    # Read only the measured region. Packaged prefix PCM can be sparse.
    region_start = int(offsets[0])
    region = pcm[region_start:int(offsets[-1]) + frame]
    material = region[(offsets - region_start)[:, None] + np.arange(frame)]
    energy = np.mean(material ** 2, axis=1)
    spectrum = np.abs(np.fft.rfft(material * np.hanning(frame), axis=1)) ** 2
    high = np.sum(spectrum[:, np.fft.rfftfreq(frame, 1/44100) >= 3000], axis=1)
    high /= frame * frame
    db = 10 * np.log10(np.maximum(energy, 1e-14))
    high_db = 10 * np.log10(np.maximum(high, 1e-14))
    groups = []
    for i, at in enumerate(offsets):
        if not (start <= at and at + frame < stop and db[i] <= -28 and high_db[i] <= -63):
            continue
        if groups and i == groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    candidates = []
    for group in groups:
        first, last = group[0], group[-1]
        lo, hi = int(offsets[first]), int(offsets[last] + frame)
        if hi - lo < 441:
            continue
        before = (offsets + frame <= lo) & (offsets >= lo - side)
        after = (offsets >= hi) & (offsets + frame <= hi + side)
        if not before.any() or not after.any():
            continue
        floor = float(np.median(db[group]))
        high_floor = float(np.median(high_db[group]))
        rise = min(float(np.max(db[before])), float(np.max(db[after]))) - floor
        release = min(float(np.max(high_db[before])), float(np.max(high_db[after]))) - high_floor
        if rise < 7 or release < 12:
            continue
        mid = (lo + hi) // 2
        half = min(132, (hi - lo) // 4)
        cut = mid - half + int(np.argmin(np.abs(pcm[mid-half:mid+half+1])))
        candidates.append((cut, [lo,hi], round(rise,2), round(release,2)))
    return candidates[0] if len(candidates) == 1 else None


def connected_stop(source, source_words, donor, first, returned_first):
    """Separate a final voiced stop from a new voiced stop at their measured closures."""
    if first == 0 or returned_first == 0:
        return None
    words = donor["phones"]
    source_fixed, return_fixed = source_words[first-1], words[returned_first-1]
    if source_fixed["word"].casefold() != return_fixed["word"].casefold():
        return None
    source_tail = source_fixed["phonemes"][-1]
    return_tail = return_fixed["phonemes"][-1]
    target = words[returned_first]["phonemes"][0]
    if (source_tail["phone"] != return_tail["phone"] or source_tail["phone"] not in {"B", "D", "G"}
            or target["phone"] not in {"B", "D", "G"} or target["phone"] == return_tail["phone"]
            or source_words[first]["phonemes"][0]["phone"] not in {"AH", "AE", "AX"}
            or return_tail["end_sample"] != target["start_sample"]):
        return None
    src = closure_in_phone(source, source_tail)
    take = closure_in_phone(donor["samples"], target)
    if src is None or take is None:
        return None
    return src[0], take[0], dict(method="voiced_stop_release", source_cut_sample=src[0],
        return_cut_sample=take[0], fixed_phone=source_tail["phone"], replacement_phone=target["phone"],
        source_closure_samples=src[1], return_closure_samples=take[1],
        source_level_recovery_db=src[2:], return_level_recovery_db=take[2:])


def exact_join_options(base, base_phones, donor, index, *, leading):
    """Find cuts at one occurrence boundary, using quiet or matching phones."""
    donor_index = index - donor['begin'] + (0 if leading else donor['extra'])
    donor_phones = donor['phones']
    if not 0 <= donor_index <= len(donor_phones):
        return
    if not leading and 'following_cue' in donor and index == donor['following_cue']['index']:
        cue = donor['following_cue']
        source, returned = cue['source'], cue['return']
        yield source['cut_sample'], returned['cut_sample'], dict(
            method='phoneme_quiet_boundary', sample_rate=RATE,
            source_cut_sample=source['cut_sample'], return_cut_sample=returned['cut_sample'],
            source_phone_edges=adjacent_phone_edges(base_phones, index),
            return_phone_edges=adjacent_phone_edges(donor_phones, donor_index),
            source_waveform_quiet_samples=source['quiet_samples'],
            return_waveform_quiet_samples=returned['quiet_samples'], following_cue=cue,
        )
        return
    side = 'before' if leading else 'after'
    source_edge = index in (0, len(base_phones))
    gap = None if source_edge else quiet_boundary(base, base_phones, index)
    if source_edge:
        source_cut = 0 if index == 0 else len(base)
    else:
        source_cut = quiet_cut(gap, side) if gap else None
    returned_cut = donor['starts' if leading else 'ends'].get(donor_index)
    closure = None
    if leading and gap and returned_cut is None:
        closure = voiced_stop_closure(donor['samples'], donor_phones, donor_index, base_phones, index)
        if closure is not None:
            returned_cut = quiet_cut(closure, side)
    quiet_donor = False
    if source_cut is not None and returned_cut is not None:
        donor_edge = donor_index in (0, len(donor_phones))
        trimmed = 0 < returned_cut < len(donor['samples'])
        quiet_donor = source_edge or not donor_edge or trimmed
        if quiet_donor:
            if donor_index == len(donor_phones) and not leading and trimmed:
                terminal = donor.get('terminal_decay') or terminal_quiet_run(
                    donor['samples'], donor_phones[-1]['phonemes'][-1])
                donor_gap = terminal if terminal and terminal[0] < returned_cut < terminal[1] else None
            elif donor_index == 0 and leading and 'initial_quiet' in donor:
                donor_gap = donor['initial_quiet']
            elif closure is not None:
                donor_gap = closure
            else:
                donor_gap = None if donor_edge else quiet_boundary(donor['samples'], donor_phones, donor_index)
            evidence = dict(method='phoneme_quiet_boundary', sample_rate=RATE,
                            source_cut_sample=source_cut, return_cut_sample=returned_cut,
                            source_phone_edges=adjacent_phone_edges(base_phones, index),
                            return_phone_edges=adjacent_phone_edges(donor_phones, donor_index))
            if gap is not None:
                evidence['source_waveform_quiet_samples'] = list(gap[:2])
            if closure is not None:
                evidence['return_closure_phone'] = donor_phones[donor_index]['phonemes'][0]['phone']
            if donor_gap is not None:
                evidence['return_waveform_quiet_samples'] = list(donor_gap[:2])
                if donor_index == len(donor_phones) and not leading and donor.get('terminal_decay'):
                    evidence['return_terminal_recognition_sha256'] = donor['terminal_recognition_sha256']
                    if donor.get('terminal_quiet_rule'):
                        evidence['return_terminal_quiet_rule'] = donor['terminal_quiet_rule']
            yield source_cut, returned_cut, evidence

    for connected_cut, connected_return, connected in connected_join_options(
            base, base_phones, donor, index, donor_index, leading=leading):
        yield connected_cut, connected_return, connected
        # Match an existing quiet cut when the only difference is a short
        # stretch of measured source silence outside both phonemes.
        if gap is None or source_cut is None:
            continue
        start, end, _ = gap
        fixed = connected['source_fixed_phone_edge_sample']
        pause = connected_cut - source_cut if leading else source_cut - connected_cut
        if (0 < pause <= HANDLE and start < source_cut < end
                and start < connected_cut < end
                and min(fixed, connected_cut) <= source_cut <= max(fixed, connected_cut)):
            yield source_cut, connected_return, dict(
                connected, source_cut_sample=source_cut,
                source_pause_samples=abs(fixed - source_cut),
                source_waveform_quiet_samples=[start, end],
                source_phone_cut_sample=connected_cut,
            )
    if leading and gap is None:
        closure = stop_closure_join(base_phones, donor, index, donor_index)
        if closure is not None:
            yield closure['source_cut_sample'], closure['return_cut_sample'], closure
    if quiet_donor and gap:
        start, end, clock = gap
        # The phone boundary can be shared with connected takes when it lies
        # inside the same quiet run. Keep a measured 5 ms margin on both sides.
        boundary = evidence["source_phone_edges"]["onset_sample" if leading else "release_sample"]
        for anchor in dict.fromkeys((clock, boundary)):
            if (anchor is not None and anchor != source_cut
                    and start + QUIET_CLOCK_MARGIN <= anchor <= end - QUIET_CLOCK_MARGIN):
                yield anchor, returned_cut, dict(evidence, source_cut_sample=anchor,
                                                source_quiet_samples=[start, end])
    if donor.get('base_choice_origin') and gap:
        # A verbatim source slice can share a measured quiet cut with the
        # generated takes. Its donor clock is the same as the source clock.
        start, end, clock = gap
        cuts = [source_cut]
        if clock != source_cut and start + QUIET_CLOCK_MARGIN <= clock <= end - QUIET_CLOCK_MARGIN:
            cuts.append(clock)
        edges = adjacent_phone_edges(base_phones, index)
        for cut in cuts:
            yield cut, cut, dict(method='phoneme_quiet_boundary', sample_rate=RATE,
                                 source_cut_sample=cut, return_cut_sample=cut,
                                 source_phone_edges=edges, return_phone_edges=edges,
                                 source_waveform_quiet_samples=[start, end],
                                 return_waveform_quiet_samples=[start, end])
    side_key = 'adjacent_lead' if leading else 'adjacent_tail'
    if donor.get(side_key) == index and 'adjacent_observation' in donor:
        source_edges = adjacent_phone_edges(base_phones, index)
        release = source_edges['release_sample']
        if release is None or release != source_edges['onset_sample']:
            return
        boundary = adjacent_donor_cut(donor['samples'], donor_phones, donor_index,
                                      donor['adjacent_observation'], leading=leading)
        if boundary:
            cut, quiet = boundary
            return_edges = adjacent_phone_edges(donor_phones, donor_index)
            yield release, cut, dict(
                method='phoneme_adjacent_delimiters', sample_rate=RATE,
                source_cut_sample=release, return_cut_sample=cut,
                source_phone_edges=source_edges, return_phone_edges=return_edges,
                source_adjacent_word_index=index, return_adjacent_word_index=donor_index,
                return_waveform_quiet_samples=list(quiet),
                return_adjacent_recognition_sha256=donor['adjacent_recognition_sha256'],
            )


def stop_closure_join(base_phones, donor, index, donor_index):
    """Keep the source fricative and begin the replacement in its own stop closure."""
    returned = donor['phones']
    if not (0 < index < len(base_phones) and 0 < donor_index < len(returned)):
        return None
    source_fixed, return_fixed = base_phones[index - 1], returned[donor_index - 1]
    if lexical_text(source_fixed['word']) != lexical_text(return_fixed['word']):
        return None
    source_edges = adjacent_phone_edges(base_phones, index)
    return_edges = adjacent_phone_edges(returned, donor_index)
    source_cut, return_cut = source_edges['onset_sample'], return_edges['onset_sample']
    if (source_cut is None or return_cut is None
            or source_cut != source_edges['release_sample'] or return_cut != return_edges['release_sample']):
        return None
    source_tail = source_fixed['phonemes'][-1]['phone']
    return_tail = return_fixed['phonemes'][-1]['phone']
    stop = returned[donor_index]['phonemes'][0]
    if source_tail != return_tail or source_tail not in VOICED_FRICATIVES or stop['phone'] not in VOICELESS_STOPS:
        return None
    silence = quiet_runs(donor['samples'], return_cut, stop['end_sample'], minimum_frames=4)
    if not silence or silence[0][0] != return_cut or silence[0][1] - return_cut < STOP_CLOSURE_MINIMUM:
        return None
    # Require at least one active frame in the aligned stop after the closure.
    if silence[0][1] + FRAME > stop['end_sample']:
        return None
    return dict(method='phoneme_stop_closure', sample_rate=RATE, source_cut_sample=source_cut,
                return_cut_sample=return_cut, source_phone_edges=source_edges, return_phone_edges=return_edges,
                fixed_release_phone=source_tail, replacement_onset_phone=stop['phone'],
                stop_closure_samples=list(silence[0]), return_stop_end_sample=stop['end_sample'])


def connected_join_options(base, base_phones, donor, index, donor_index, *, leading):
    donor_phones = donor['phones']
    if not (0 < index < len(base_phones) and 0 < donor_index < len(donor_phones)):
        return
    fixed_offset, variable_offset = (-1, 0) if leading else (0, -1)
    source_word = base_phones[index + fixed_offset]
    donor_word = donor_phones[donor_index + fixed_offset]
    if lexical_text(source_word['word']) != lexical_text(donor_word['word']):
        return
    source_phones = base_phones[index + variable_offset]['phonemes']
    returned_phones = donor_phones[donor_index + variable_offset]['phonemes']
    if not source_phones or not returned_phones:
        return
    edge = 0 if leading else -1
    source_phone, returned_phone = source_phones[edge], returned_phones[edge]
    try:
        check = _join(source_word, donor_word, source_phone, returned_phone,
                      at_lead=leading, source_pcm=base, context_pcm=donor['samples'],
                      language='en-US', phone_set='arpabet')
    except PhonemeJoinRejected:
        return
    if check['acoustic_check'] == 'compared':
        sample = 'start_sample' if leading else 'end_sample'
        source_cut, return_cut = source_phone[sample], returned_phone[sample]
        fixed_edge = 'end_sample' if leading else 'start_sample'
        source_fixed = source_word['phonemes'][-1 if leading else 0][fixed_edge]
        return_fixed = donor_word['phonemes'][-1 if leading else 0][fixed_edge]
        yield source_cut, return_cut, dict(
            check, method='connected_phonemes', sample_rate=RATE,
            source_cut_sample=source_cut, return_cut_sample=return_cut,
            source_phone_edges=adjacent_phone_edges(base_phones, index),
            return_phone_edges=adjacent_phone_edges(donor_phones, donor_index),
            source_fixed_phone_edge_sample=source_fixed, return_fixed_phone_edge_sample=return_fixed,
        )



def _sibilant_onset(samples, start, phone_end, search_seconds):
    """Refine a nasal release using sustained /s/ friction on the same clock."""
    end = min(phone_end, start + round(search_seconds * RATE))
    width = round(.008 * RATE)
    hop = round(.002 * RATE)
    signal = samples[start:end]
    taper = np.hanning(width)
    high = np.fft.rfftfreq(2048, 1 / RATE) >= 2500
    fractions = []
    for offset in range(0, len(signal) - width + 1, hop):
        frame = signal[offset:offset + width]
        power = np.abs(np.fft.rfft(frame * taper, 2048)) ** 2
        level = float(np.mean(frame ** 2))
        fractions.append(float(power[high].sum() / max(power.sum(), 1e-14)) if level > 1e-5 else 0)
    if len(fractions) < 4 or fractions[0] > .15:
        return None
    for index in range(1, len(fractions) - 2):
        if min(fractions[index:index + 3]) >= .45:
            return start + index * hop + width // 2
    return None


def nasal_sibilant_name_lead(base, returned, source, take, source_phone, return_phone, lead):
    """Keep the original nasal release and blend its onset into the returned /s/."""
    pair = lead.get("phones", ())
    if (lead.get("method") != "connected_phonemes" or pair != lead.get("source_phones")
            or len(pair) != 2 or pair[0] not in {"M", "N", "NG"} or pair[1] != "S"
            or source_phone["phone"] != "S" or return_phone["phone"] != "S"):
        return None
    start, stop = source
    begin, end = take
    if (source_phone["start_sample"], return_phone["start_sample"]) != (start, begin):
        return None
    # Existing compact plans carry 45 ms of base samples at this boundary.
    # The complete return can be searched a little farther within the same /s/.
    source_cut = _sibilant_onset(base, start, min(source_phone["end_sample"], stop), .045)
    return_cut = _sibilant_onset(returned, begin, min(return_phone["end_sample"], end), .06)
    if source_cut is None or return_cut is None:
        return None
    overlap = round(.002 * RATE)
    if (source_cut - start <= overlap or return_cut + overlap >= return_phone["end_sample"]
            or return_cut + overlap >= end):
        return None
    original = base[start:source_cut]
    ramp = np.linspace(0, 1, overlap, dtype=np.float32)
    blended = original[-overlap:] * (1 - ramp) + returned[return_cut:return_cut + overlap] * ramp
    result = np.concatenate((original[:-overlap], blended, returned[return_cut + overlap:end]))
    return result, dict(method="nasal_sibilant_spectral_onset", sample_rate=RATE,
                        source_cut_sample=source_cut, return_cut_sample=return_cut,
                        copied_source_samples=[start, source_cut], overlap_samples=overlap)


def join_windows(words, row, spans, frames, leading_word_counts=None):
    """Cover waveform quiet searches, phoneme comparisons and join ramps."""
    from lite_portable_constraints import leading_word_count, name_occurrences
    from lite_portable_render import count

    windows = []
    transition = round(max(_TRANSITION_WINDOWS_MS) * RATE / 1000)
    quiet = round(.045 * RATE)
    for index, (slot, (start, end)) in enumerate(zip(name_occurrences(row), spans, strict=True)):
        text = slot["stock_text"]
        first, last = (count(text[:point]) for point in slot["slot"])
        native_leader = leading_word_count(slot)
        leader = leading_word_counts[index] if leading_word_counts is not None else native_leader
        if (type(leader) is not int or not native_leader <= leader <= first
                or not first - native_leader < last <= len(words) or not 0 <= start < end <= frames):
            raise ValueError("The measured join is outside the source words or sample clock.")
        windows.extend(((max(0, start - 1), min(frames, start + 1)),
                        (max(0, end - 1), min(frames, end + 1))))
        boundaries = [(first - native_leader, True), (last, False)]
        if leader != native_leader:
            boundaries.append((first - leader, True))
        for edge, leading in boundaries:
            if edge in (0, len(words)):
                continue
            left, right = words[edge - 1], words[edge]
            old = left["phonemes"][-1] if left["phonemes"] else left
            new = right["phonemes"][0] if right["phonemes"] else right
            release, onset = old["end_sample"], new["start_sample"]
            windows.append((max(old["start_sample"], release - quiet),
                            min(new["end_sample"], onset + quiet)))
            if left["phonemes"] and right["phonemes"]:
                if leading:
                    windows.append((max(old["start_sample"], release - _ACOUSTIC_WINDOW), release))
                    cut = onset
                else:
                    windows.append((onset, min(new["end_sample"], onset + _ACOUSTIC_WINDOW)))
                    cut = release
                windows.append((max(old["start_sample"], cut - transition),
                                min(new["end_sample"], cut + transition)))
    merged = []
    for start, end in sorted(windows):
        if end <= start:
            continue
        if not 0 <= start < end <= frames:
            raise ValueError("A measured source window is outside its sample clock.")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


class JoinWaveform:
    """Provide measured sample ranges without synthesizing missing samples."""

    def __init__(self, segments, frames):
        self.segments = segments
        self.starts = [start for start, _ in segments]
        self.frames = frames

    def __len__(self):
        return self.frames

    def __getitem__(self, key):
        scalar = isinstance(key, int)
        if scalar:
            start, end = key, key + 1
        else:
            start, end, stride = key.indices(self.frames)
            if stride != 1:
                raise ValueError("The join needs a contiguous sample range.")
        if not 0 <= start <= end <= self.frames:
            raise ValueError("The join is outside the paced sample clock.")
        if start == end:
            return np.empty(0, dtype=np.float32)
        index = bisect_right(self.starts, start) - 1
        if index < 0:
            raise ValueError("The join needs an unmeasured source sample.")
        origin, pcm = self.segments[index]
        if end > origin + len(pcm):
            raise ValueError("The join needs an unmeasured source sample.")
        result = pcm[start - origin:end - origin]
        return result[0] if scalar else result


def measured_word_cut(words, index, frames, *, leading, preferred=None):
    """Choose a cut that includes the entire owned phone and excludes the adjacent word."""
    if not (0 <= index < len(words) if leading else 0 < index <= len(words)):
        return None
    adjacent = words[max(0, index - 1):index + 1]
    if any(not w["phonemes"] or any(p["phone"].casefold() in UNKNOWN_PHONES for p in w["phonemes"])
           for w in adjacent):
        return None
    edges = adjacent_phone_edges(words, index)
    release = edges["release_sample"] if index else 0
    onset = edges["onset_sample"] if index < len(words) else frames
    if not 0 <= release <= onset <= frames:
        return None
    if preferred is not None and release <= preferred <= onset:
        return preferred
    return onset if leading else release


def splice(base, base_phones, donor, begin, end, slot_begin, slot_end, source_bounds, *,
           name_only=False, existing_leader=False, article=None):
    """Use the existing anchor where practical. Keep a connected word intact if needed."""
    options = []
    if existing_leader:
        spans = ((int(existing_leader), 0),)
    else:
        spans = ((0, 0),) if name_only else ((0, 0), (1, 0), (0, 1), (1, 1))
    for include_left, include_right in spans:
        first, last = slot_begin - include_left, slot_end + include_right
        if not begin <= first < last <= end:
            continue
        donor_first = first - donor["begin"]
        donor_last = last - donor["begin"] + donor["extra"]
        source_context = [w["word"].casefold() for w in (
            base_phones[first:slot_begin] + base_phones[slot_end:last])]
        returned_context = [w["word"].casefold() for w in (
            donor["phones"][donor_first:donor_first + include_left]
            + donor["phones"][donor_last - include_right:donor_last])]
        article_matches = (article and include_left == 1 and include_right == 0
                           and source_context == [article["source"].casefold()]
                           and returned_context == [article["spoken"].casefold()])
        if source_context != returned_context and not article_matches:
            continue
        for a, c, lead in exact_join_options(base, base_phones, donor, first, leading=True):
            for b, d, tail in exact_join_options(base, base_phones, donor, last, leading=False):
                may_extend = include_left and not existing_leader
                if not (a <= source_bounds[0] if may_extend else a == source_bounds[0]):
                    continue
                if not (b >= source_bounds[1] if include_right else b == source_bounds[1]):
                    continue
                if not 0 <= a < b <= len(base) or not 0 <= c < d <= len(donor["samples"]):
                    continue
                joins = (lead, tail)
                connected = sum(check["method"] == "connected_phonemes" for check in joins)
                # Only rejoin a wider phrase at quiet or at the recording edge.
                if ((may_extend and lead["method"] == "connected_phonemes")
                        or (include_right and tail["method"] == "connected_phonemes")):
                    continue
                score = (connected, include_left + include_right, b - a)
                options.append((score, (a, b), (c, d), dict(lead=lead, tail=tail)))
        if include_left == int(existing_leader) and not include_right:
            # The fixed clips already own the samples outside the authored
            # span. Their endpoint positions do not constrain the donor clock.
            frames = len(donor["samples"])
            c = measured_word_cut(donor["phones"], donor_first, frames, leading=True,
                                  preferred=donor["starts"].get(donor_first))
            d = measured_word_cut(donor["phones"], donor_last, frames, leading=False,
                                  preferred=donor["ends"].get(donor_last))
            a, b = source_bounds
            if c is not None and d is not None and 0 <= a < b <= len(base) and c < d:
                edges = {}
                for side, bound, cut, index, returned_index in (
                    ("lead", a, c, first, donor_first), ("tail", b, d, last, donor_last)
                ):
                    edges[side] = dict(method="independent_name_boundary", sample_rate=RATE,
                                       source_cut_sample=bound, return_cut_sample=cut,
                                       source_phone_edges=adjacent_phone_edges(base_phones, index),
                                       return_phone_edges=adjacent_phone_edges(donor["phones"], returned_index))
                options.append(((3, include_left, b - a), (a, b), (c, d), edges))
    return sorted(options, key=lambda option: option[0])



def trim_donor(samples, phones, starts, ends):
    """Use only measured quiet at the edges of a contextual name return."""
    initial = phones[0]["phonemes"][0]
    final = phones[-1]["phonemes"][-1]
    if 0 in starts:
        quiet = quiet_runs(samples, 0, initial["start_sample"], minimum_frames=4)
        if quiet and quiet[0][0] == 0:
            starts[0] = max(0, quiet[0][1] - HANDLE)
    if len(phones) in ends:
        quiet = terminal_quiet_run(samples, final)
        if quiet is None:
            ends[len(phones)] = None
            return
        manner = join_rule(final["phone"], None, language="en-US", phone_set="arpabet").get("outgoing_manner")
        resonant = manner in {"vowel", "nasal", "liquid approximant", "glide approximant"}
        if resonant and quiet_runs(samples, final["end_sample"], quiet[0]):
            # Resonant speech does not release after a silent stop closure.
            # A second sound after measured quiet needs a new alignment or take.
            ends[len(phones)] = None
            return
        ends[len(phones)] = min(quiet[1] - 1, quiet[0] + HANDLE)


def join_discontinuities(mix, joins):
    """Flag isolated sample jumps for review against the surrounding waveform."""
    margin = round(RATE * .020)
    suspects = []
    for point in joins:
        if point < margin or point + margin > len(mix):
            continue
        steps = np.abs(np.diff(mix[point - margin:point + margin]))
        jump = float(steps[margin - 1])
        if jump <= .01:
            continue
        neighbors = np.concatenate((steps[:margin - 1], steps[margin:]))
        nearby = float(np.quantile(neighbors, .99))
        if jump > 3 * nearby:
            suspects.append(dict(sample=point, seconds=point / RATE, step=jump,
                                 neighboring_step_p99=nearby))
    return suspects
