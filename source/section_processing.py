"""Retain the signal history of a source split without baking or changing media.

Sections reference an original processing window. Render that window with the
current authored controls, then take the section's portion. Only its final
section owns the effect tail. Moving/copying a section preserves its sound;
changing effects remains reversible and reprocesses from original media.
"""
from audio_effects import apply_effects
from stereo_frames import frame_count, trim_frames


def process(source, channels, layer, track, begin, end, stretch, envelope, integer):
    speed, gain = layer.get('playback_speed', 1), layer.get('gain_db', 0)
    context = layer.get('processing_context')
    lo, hi = begin, end
    fade_in, fade_out = layer['fade_in_samples'], layer['fade_out_samples']
    if context is not None:
        if not isinstance(context, dict):raise ValueError('Invalid clip processing context.')
        limit = frame_count(source, channels)
        lo = integer(context.get('source_in_sample'), 'processing source in', 0, limit)
        hi = integer(context.get('source_out_sample'), 'processing source out', lo, limit)
        if hi == lo:raise ValueError('Clip processing context must contain source audio.')
        # Extending a trimmed section remains nondestructive. Expand its own
        # window while leaving other sections and the saved original unchanged.
        lo, hi = min(lo, begin), max(hi, end)
        context_fade_in = integer(context.get('fade_in_samples', 0), 'processing fade in')
        context_fade_out = integer(context.get('fade_out_samples', 0), 'processing fade out')
        if begin != lo:fade_in = context_fade_in
        if end != hi:fade_out = context_fade_out
    origin_fade_in, origin_fade_out = fade_in, fade_out
    chunk = stretch(trim_frames(source, channels, lo, hi), speed, channels)
    full_dry = frame_count(chunk, channels)
    chunk, info = apply_effects(envelope(chunk, channels, fade_in, fade_out, gain), channels, layer.get('effects'))
    bypass = layer.get('bypass_track_effects', False)
    if not isinstance(bypass, bool):raise ValueError('Clip track-effect bypass must be true or false.')
    chain = [] if bypass or track.get('effects_bypassed', False) else track.get('effects', [])
    chunk, track_info = apply_effects(chunk, channels, chain)
    info['effect_tail_sample_count'] += track_info['effect_tail_sample_count']
    dry = full_dry
    full_total = frame_count(chunk, channels)
    first, stop = 0, full_total
    if context is not None:
        envelopes = context.get('section_envelopes', [])
        if not isinstance(envelopes, list):raise ValueError('Invalid section envelopes.')
        for item in envelopes:
            if not isinstance(item, dict):raise ValueError('Invalid section envelope.')
            left = integer(item.get('source_in_sample'), 'section source in', lo, hi)
            right = integer(item.get('source_out_sample'), 'section source out', left, hi)
            fade_in = integer(item.get('fade_in_samples', 0), 'section fade in')
            fade_out = integer(item.get('fade_out_samples', 0), 'section fade out')
            first = round((left-lo)*full_dry/(hi-lo))
            stop = frame_count(chunk, channels) if right == hi else round((right-lo)*full_dry/(hi-lo))
            chunk[first*channels:stop*channels] = envelope(
                trim_frames(chunk, channels, first, stop), channels, fade_in, fade_out, 0)
        first = round((begin-lo)*full_dry/(hi-lo))
        last = round((end-lo)*full_dry/(hi-lo))
        dry = last-first
        owns_tail = end == hi
        stop = frame_count(chunk, channels) if owns_tail else last
        chunk = trim_frames(chunk, channels, first, stop)
        # Newly authored fades at an interior section edge are deliberate edits.
        # Original outer fades are already present in the processed source.
        chunk = envelope(chunk, channels, layer['fade_in_samples'] if begin != lo else 0,
                         layer['fade_out_samples'] if end != hi else 0, 0)
        if not owns_tail:
            info['effect_tail_sample_count'] = 0
            track_info['effect_tail_sample_count'] = 0
        info['dry_rendered_sample_count'] = dry
    # Exact coordinates in the original processed signal. Section joins use
    # these measured offsets rather than estimating tempo or effect-tail sizes.
    info['section_origin'] = {
        'source_in_sample': lo, 'source_out_sample': hi,
        'fade_in_samples': origin_fade_in, 'fade_out_samples': origin_fade_out,
        'dry_sample_count': full_dry, 'sample_count': full_total,
        'section_start_sample': first, 'section_end_sample': stop,
    }
    info.update(track_effects=track_info['effects'],
                track_effect_tail_sample_count=track_info['effect_tail_sample_count'],
                edge_fade_out_samples=0 if info['effect_tail_sample_count'] else layer['fade_out_samples'])
    from section_effects import process as process_sections
    return process_sections(chunk,channels,layer,info,dry,envelope,integer)
