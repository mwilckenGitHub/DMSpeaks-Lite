"""contextual_layer_layout.py

Reusable DMSpeaks normal-layer graph constructor for the accepted native-infill
assembly procedure.

API:
    build_context_layout(source, context, source_window, word_bounds, *,
                         generated_window, mode='source', slot=None)

Returns a dict with keys: tracks, layers, provenance.
"""

import copy

RATE = 44100


def _validate_asset(asset, label):
    """Validate an asset metadata dict. Returns (id, sample_count)."""
    if not isinstance(asset, dict):
        raise ValueError(f"{label} must be a dict.")
    aid = asset.get('id')
    if not isinstance(aid, str) or not aid:
        raise ValueError(f"{label} requires a nonempty string 'id'.")
    sr = asset.get('sample_rate')
    if isinstance(sr, bool) or not isinstance(sr, int) or sr != RATE:
        raise ValueError(f"{label} sample_rate must be {RATE}.")
    ch = asset.get('channels', 1)
    if isinstance(ch, bool) or not isinstance(ch, int) or ch != 1:
        raise ValueError(f"{label} channels must be 1.")
    sc = asset.get('sample_count')
    if isinstance(sc, bool) or not isinstance(sc, int) or sc <= 0:
        raise ValueError(f"{label} sample_count must be a positive integer.")
    return aid, sc


def _validate_window(window, label, upper):
    """Validate a (a, b) sample interval. Returns (a, b)."""
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise ValueError(f"{label} must be a 2-element sequence.")
    a, b = window
    for v, name in ((a, 'start'), (b, 'end')):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{label} {name} must be an integer.")
    if a <= 0 or b <= 0:
        raise ValueError(f"{label} values must be positive.")
    if a >= b:
        raise ValueError(f"{label} start must be less than end.")
    if b > upper:
        raise ValueError(f"{label} end must not exceed {upper}.")
    return a, b


def _sha256_of_asset(asset):
    """Retain the actual media digest; metadata hashes are not audio identity."""
    value = asset.get('sha256')
    if value is not None and (not isinstance(value, str) or len(value) != 64
                              or any(c not in '0123456789abcdef' for c in value)):
        raise ValueError('Invalid media sha256.')
    return value


def _make_layer(layer_id, track_id, asset_id, src_in, src_out, slot_order,
                anchor=None, after=None, gap=0, overlap=0, overlap_mode='mix',
                fade_in=0, fade_out=0, slot=None):
    """Build a single normal-layer dict compatible with render_layers."""
    layer = {
        'id': layer_id,
        'track_id': track_id,
        'asset_id': asset_id,
        'kind': 'audio',
        'source_in_sample': src_in,
        'source_out_sample': src_out,
        'slot_order': slot_order,
        'gap_samples': gap,
        'overlap_samples': overlap,
        'overlap_mode': overlap_mode,
        'fade_in_samples': fade_in,
        'fade_out_samples': fade_out,
        'playback_speed': 1.0,
        'gain_db': 0.0,
        'muted': False,
    }
    if anchor is not None:
        layer['anchor_start_sample'] = anchor
    if after is not None:
        layer['after_layer_id'] = after
    if slot is not None:
        layer['slot'] = copy.deepcopy(slot)
    return layer


def resolve_base_join(base_join, *, source_end, source_count, generated_start,
                      generated_end, word_start):
    """Choose explicit BASE ownership; never infer words from total duration."""
    if base_join is None:
        return generated_end, source_end
    if not isinstance(base_join, dict) or set(base_join) != {'returned_end_sample', 'source_resume_sample'}:
        raise ValueError('Supply both returned-end and source-resume sample coordinates.')
    end, resume = base_join['returned_end_sample'], base_join['source_resume_sample']
    if type(end) is not int or type(resume) is not int:
        raise ValueError('Base join coordinates must be integers, not booleans.')
    if not generated_start < end <= generated_end or not word_start < end:
        raise ValueError('The base join must lie in the generated region after the word starts.')
    if not source_end <= resume <= source_count:
        raise ValueError('Resume original base speech at or after the removed source window.')
    return end, resume


def build_context_layout(source, context, source_window, word_bounds, *,
                         generated_window, mode='source', slot=None, base_join=None):
    """
    Build a reusable normal-layer graph for native-infill assembly.

    Parameters
    ----------
    source : dict
        Asset metadata for the source (removed) audio.
    context : dict
        Asset metadata for the complete reconstructed context PCM.
    source_window : (int, int)
        (a, b) actual removed SOURCE sample interval, 0 < a < b < S.
    word_bounds : (int, int)
        (wa, wb) estimated target word range in COMPLETE reconstructed context PCM.
    generated_window : (int, int)
        Actual generated range in reconstructed PCM, from returned metadata.
    mode : str
        'source' or 'context'.
    slot : dict or None
        Optional target slot description.

    Returns
    -------
    dict with keys: tracks, layers, provenance.
    """
    if mode not in ('source', 'context'):
        raise ValueError("mode must be 'source' or 'context'.")

    src_id, S = _validate_asset(source, 'source')
    ctx_id, R = _validate_asset(context, 'context')

    a, b = _validate_window(source_window, 'source_window', S)
    if b == S:
        raise ValueError('The source needs following context.')
    wa, wb = _validate_window(word_bounds, 'word_bounds', R)

    # Prefer the actual returned region. A full codec decode may retain padding
    # beyond the original source's last sample, so total duration is not proof.
    start, newgend = _validate_window(generated_window, 'generated_window', R)
    if start != a:
        raise ValueError('The generated region must start at the removed source window.')
    if R - newgend < S - b:
        raise ValueError('The reconstructed return does not cover the following source context.')
    generation_boundary_source = 'explicit returned sample coordinates'

    # Require a <= wa < newgend and wa < wb <= R
    if not (a <= wa < newgend):
        raise ValueError(f"wa={wa} must satisfy a={a} <= wa < newgend={newgend}.")
    if not (wa < wb <= R):
        raise ValueError(f"wb={wb} must satisfy wa={wa} < wb <= R.")
    if mode == 'context' and base_join is not None:
        raise ValueError('The complete reconstructed reference cannot have a base ownership edit.')
    join, resume = resolve_base_join(base_join, source_end=b, source_count=S,
                                    generated_start=a, generated_end=newgend, word_start=wa)

    # Default slot
    if slot is None:
        slot = {'order': 2, 'type': 'infill'}
    elif not isinstance(slot, dict):
        raise ValueError("slot must be a dict or None.")
    slot = copy.deepcopy(slot)
    if 'order' not in slot:
        slot['order'] = 2
    if 'type' not in slot:
        slot['type'] = 'infill'

    order = slot['order']
    if isinstance(order, bool) or not isinstance(order, int) or order < 1:
        raise ValueError("slot order must be a positive integer.")

    # Track definitions
    base_track = {'id': 'base', 'name': 'Base speech', 'muted': False, 'gain_db': 0}
    infill_track = {'id': 'infill', 'name': 'Replacement', 'muted': False, 'gain_db': 0}
    tracks = [base_track, infill_track]

    layers = []
    prev_id = None

    if mode == 'context':
        # Three partitions of context PCM:
        # 1. prefix: context[0:wa] at 0
        # 2. word: context[wa:wb] after prefix
        # 3. suffix: context[wb:R] after word (if nonempty)

        # Prefix
        if wa > 0:
            prefix = _make_layer(
                'context-prefix', 'base', ctx_id,
                src_in=0, src_out=wa, slot_order=max(1, order - 1),
                anchor=0,
                slot={'order': max(1, order - 1), 'type': 'fixed'}
            )
            layers.append(prefix)
            prev_id = 'context-prefix'

        # Word
        word = _make_layer(
            'context-word', 'infill', ctx_id,
            src_in=wa, src_out=wb, slot_order=order,
            after=prev_id if prev_id else None,
            anchor=0 if prev_id is None else None,
            slot=slot
        )
        if prev_id is None:
            word['anchor_start_sample'] = 0
        layers.append(word)
        prev_id = 'context-word'

        # Suffix
        if wb < R:
            suffix = _make_layer(
                'context-suffix', 'base', ctx_id,
                src_in=wb, src_out=R, slot_order=order + 1,
                after=prev_id,
                slot={'order': order + 1, 'type': 'fixed'}
            )
            layers.append(suffix)
            prev_id = 'context-suffix'

    elif mode == 'source':
        # Prefix: source[0:a] at 0
        if a > 0:
            prefix = _make_layer(
                'context-prefix', 'base', src_id,
                src_in=0, src_out=a, slot_order=max(1, order - 1),
                anchor=0,
                slot={'order': max(1, order - 1), 'type': 'fixed'}
            )
            layers.append(prefix)
            prev_id = 'context-prefix'

        # Leading returned fragment: context[a:wa] on BASE after prefix (if nonempty)
        if a < wa:
            leader = _make_layer(
                'context-leader', 'base', ctx_id,
                src_in=a, src_out=wa, slot_order=max(1, order - 1),
                after=prev_id if prev_id else None,
                anchor=0 if prev_id is None else None,
                slot={'order': max(1, order - 1), 'type': 'fixed'}
            )
            if prev_id is None:
                leader['anchor_start_sample'] = 0
            layers.append(leader)
            prev_id = 'context-leader'

        # Word: context[wa:wb] on infill track
        word = _make_layer(
            'context-word', 'infill', ctx_id,
            src_in=wa, src_out=wb, slot_order=order,
            after=prev_id if prev_id else None,
            anchor=0 if prev_id is None else None,
            slot=slot
        )
        if prev_id is None:
            word['anchor_start_sample'] = 0
        layers.append(word)
        prev_id = 'context-word'

        # Trailing fragment and source suffix
        if wb < join:
            # Trailing returned fragment: context[wb:newgend] on BASE after word
            tail = _make_layer(
                'context-tail', 'base', ctx_id,
                src_in=wb, src_out=join, slot_order=order + 1,
                after=prev_id,
                slot={'order': order + 1, 'type': 'fixed'}
            )
            layers.append(tail)
            prev_id = 'context-tail'

            # Source suffix: source[b:S] follows trailing fragment
            if resume < S:
                suffix = _make_layer(
                    'context-suffix', 'base', src_id,
                    src_in=resume, src_out=S, slot_order=order + 1,
                    after=prev_id,
                    slot={'order': order + 1, 'type': 'fixed'}
                )
                layers.append(suffix)
                prev_id = 'context-suffix'
        else:
            # wb >= newgend: source suffix follows word with overlap
            overlap = wb - join
            if resume < S:
                suffix = _make_layer(
                    'context-suffix', 'base', src_id,
                    src_in=resume, src_out=S, slot_order=order + 1,
                    after=prev_id,
                    overlap=overlap,
                    overlap_mode='mix',
                    slot={'order': order + 1, 'type': 'fixed'}
                )
                layers.append(suffix)
                prev_id = 'context-suffix'

    # Provenance
    provenance = {
        'source_window': [a, b],
        'new_generated_window': [a, newgend],
        'word_window': [wa, wb],
        'mode': mode,
        'word_ownership_review_required': True,
        'scope': 'one native edit region',
        'source_sha256': _sha256_of_asset(source),
        'context_sha256': _sha256_of_asset(context),
        'generation_boundary_source': generation_boundary_source,
        'base_join': {'returned_end_sample': join, 'source_resume_sample': resume},
        'base_join_source': 'explicit ownership edit' if base_join is not None else 'codec partition; neighbor ownership needs review',
    }

    return {
        'tracks': tracks,
        'layers': layers,
        'provenance': provenance,
    }
