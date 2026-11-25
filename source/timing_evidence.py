"""Retained returned word labels, distinct from editable working coordinates.

No transcription, alignment, phoneme inference, or model loading takes place.
Coordinates remain in the observation's original audio clock. A derived piece
records its source offset and selected word indices without rewriting labels.
"""
import copy
import math
import re

FORMAT='dmspeaks-word-timing-evidence'


def capture(asset, words, timing_source, metadata=None, *, channel='generation'):
    if channel not in ('generation','alignment'):raise ValueError('Unknown timing evidence channel.')
    metadata=metadata or {}
    result=copy.deepcopy(asset.get('word_timing_evidence') or {})
    if result.get('format')!=FORMAT:
        result={'format':FORMAT,'version':'0.1','observations':{},'source_offset_seconds':0}
    # The initial adapter result is retained across subsequent re-alignment.
    # Each explicit re-alignment replaces only the latest alignment observation.
    if channel=='generation' and channel in result['observations']:return result
    raw=metadata.get('raw_words')
    if not isinstance(raw,list) or len(raw)!=len(words):raw=words;representation='adapter_returned_words'
    else:representation='raw_aligner_words'
    observation={
        'words':copy.deepcopy(raw),'timing_source':timing_source,
        'representation':representation,'metadata':copy.deepcopy({k:v for k,v in metadata.items() if k!='raw_words'}),
        'audio_sha256':asset.get('sha256'),'sample_rate':asset.get('sample_rate'),
        'sample_count':asset.get('sample_count'),'acoustic_accuracy_verified':False}
    if raw is not words:
        raw_labels, adapter_labels = word_labels(raw), word_labels(words)
        if raw_labels and adapter_labels and raw_labels != adapter_labels:
            # The adapter supplied paired labels before mapping its raw transcript.
            observation['bound_word_labels'] = [word['word'] for word in words]
    result['observations'][channel]=observation
    result.update(bound_audio_sha256=asset.get('sha256'),source_offset_seconds=0)
    result.pop('source_word_indices',None)
    return result


def normalize_word(label):
    return re.sub(r'\W', '', label).casefold()


def word_labels(words):
    if not all(isinstance(word, dict) and isinstance(word.get('word'), str) for word in words):
        return []
    return [normalize_word(word['word']) for word in words]


def transcript_offset(working, observed):
    """Find a unique copy of the working transcript in one observation."""
    if working == observed:
        return 0
    first_match = None
    for index in range(len(observed) - len(working) + 1):
        if observed[index] == working[0] and observed[index:index + len(working)] == working:
            if first_match is not None:
                return None
            first_match = index
    return first_match


def for_piece(asset, indices, offset_seconds, digest, *, working_words=None):
    """Project word evidence using each channel's transcript and source clock."""
    groups = returned_groups(asset)
    if not groups or not indices:
        return None
    working = word_labels(working_words if working_words is not None else asset.get('words') or [])
    if not working or not all(working) or any(not 0 <= index < len(working) for index in indices):
        return None

    observations = {}
    for channel, words, _, observation in groups:
        bound_labels = observation.get('bound_word_labels')
        if (isinstance(bound_labels, list) and len(bound_labels) == len(words)
                and all(isinstance(label, str) for label in bound_labels)):
            labels = [normalize_word(label) for label in bound_labels]
        else:
            bound_labels = None
            labels = word_labels(words)
        start = transcript_offset(working, labels)
        if start is None:
            continue
        selected = [start + index for index in indices]
        piece_observation = copy.deepcopy({key: value for key, value in observation.items()
                                          if key not in ('words', 'bound_word_labels')})
        piece_observation['words'] = [copy.deepcopy(words[index]) for index in selected]
        if bound_labels is not None:
            piece_observation['bound_word_labels'] = [bound_labels[index] for index in selected]
        observations[channel] = piece_observation
    if not observations:
        return None

    evidence = asset['word_timing_evidence']
    result = copy.deepcopy({key: value for key, value in evidence.items() if key != 'observations'})
    prior = result.get('source_word_indices')
    if isinstance(prior, list) and len(prior) == len(working):
        result['source_word_indices'] = [prior[index] for index in indices]
    else:
        result['source_word_indices'] = list(indices)
    result.update(observations=observations, bound_audio_sha256=digest,
                  source_offset_seconds=groups[0][2] + offset_seconds)
    return result


def returned_groups(asset):
    """Fail closed for unbound/changed audio; never relabel legacy edits as raw."""
    evidence=asset.get('word_timing_evidence')
    if not isinstance(evidence,dict) or evidence.get('format')!=FORMAT:return []
    digest=evidence.get('bound_audio_sha256')
    if not digest or digest!=asset.get('sha256'):return []
    origin=evidence.get('source_offset_seconds',0)
    if type(origin) not in (int,float) or not math.isfinite(origin) or origin<0:return []
    observations=evidence.get('observations')
    if not isinstance(observations,dict):return []
    groups=[]
    for channel,observation in observations.items():
        if channel not in ('generation','alignment') or not isinstance(observation,dict):continue
        words=observation.get('words')
        if not isinstance(words,list):continue
        selected=words
        if not selected or any(not isinstance(w,dict) or not isinstance(w.get('word'),str) or
            any(type(w.get(k)) not in (int,float) or not math.isfinite(w[k]) for k in ('start','end')) or
            w['start']<0 or w['end']<w['start'] for w in selected):continue
        groups.append((channel,selected,origin,observation))
    return groups
