"""Resolve a proposed processing track's layer and time coverage.

This is an authoring planner, not an audio processor. It keeps authored clips
separate from combined coverage. Times are half-open sample-frame intervals.
"""


def plan(tracks, processor_id, duration_samples, *, direction='above', clips=None):
    if not isinstance(tracks, list) or any(not isinstance(track, dict) for track in tracks):
        raise ValueError('Choose a valid track stack.')
    ids = [track.get('id') for track in tracks]
    if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError('Track IDs must be unique and nonempty.')
    if processor_id not in ids:
        raise ValueError('The processing track is no longer in this arrangement.')
    if direction not in ('above', 'below'):
        raise ValueError('Choose layers above or below the processing track.')
    if type(duration_samples) is not int or duration_samples < 0:
        raise ValueError('The timeline duration must be a nonnegative sample count.')
    position = ids.index(processor_id)
    targets = ids[:position] if direction == 'above' else ids[position + 1:]
    if clips is None:
        authored = None
        windows = [[0, duration_samples]] if duration_samples else []
    else:
        if not isinstance(clips, list) or len(clips) > 10000:
            raise ValueError('Choose a valid list of processing clips.')
        authored = []
        clip_ids = set()
        for clip in clips:
            if (not isinstance(clip, dict) or not {'start_sample', 'end_sample'} <= set(clip)
                    or set(clip) - {'id', 'start_sample', 'end_sample'}):
                raise ValueError('Each processing clip needs a start and end.')
            ident = clip.get('id')
            if ident is not None:
                if not isinstance(ident, str) or not ident or len(ident) > 128 or ident in clip_ids:
                    raise ValueError('Processing clip IDs must be unique and nonempty.')
                clip_ids.add(ident)
            start, end = clip['start_sample'], clip['end_sample']
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration_samples:
                raise ValueError('Processing clips must be nonempty and within the timeline.')
            authored.append({**({'id': ident} if ident is not None else {}),
                             'start_sample': start, 'end_sample': end})
        windows = []
        for clip in sorted(authored, key=lambda item: (item['start_sample'], item['end_sample'])):
            start, end = clip['start_sample'], clip['end_sample']
            if windows and start <= windows[-1][1]:
                windows[-1][1] = max(windows[-1][1], end)
            else:
                windows.append([start, end])
    return {'direction': direction, 'target_track_ids': targets,
            'authored_clips': authored,
            'coverage': [{'start_sample': start, 'end_sample': end} for start, end in windows]}
