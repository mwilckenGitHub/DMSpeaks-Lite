import base64
import json

def parse_fish_sse(data: bytes) -> tuple[bytes, list[dict]]:
    if len(data) > 100 * 1024 * 1024:
        raise ValueError("Input too large")

    try:
        text = data.decode('utf-8').replace('\r\n','\n')
    except UnicodeDecodeError:
        raise ValueError("Invalid UTF-8")

    audio_parts = []
    snapshots = {}
    last_seq = -1
    last_offset = -1.0

    frames = text.split('\n\n')
    for frame in frames:
        if not frame.strip():
            continue

        event_type = 'message'
        data_lines = []

        lines = frame.split('\n')
        for line in lines:
            if line.startswith(':'):
                continue
            if line.startswith('event:'):
                event_type = line[6:].strip()
            elif line.startswith('data:'):
                data_lines.append(line[5:].strip())

        if event_type == 'finish':
            continue

        if not data_lines:
            continue

        data_str = '\n'.join(data_lines)

        if data_str == '[DONE]':
            continue

        try:
            payload = json.loads(data_str)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON")

        if not isinstance(payload, dict):
            raise ValueError("Invalid payload")

        if 'error' in payload:
            raise ValueError("API error")

        if event_type == 'error':
            raise ValueError("Error event")

        chunk_seq = payload.get('chunk_seq')
        if chunk_seq is not None:
            if isinstance(chunk_seq, bool) or not isinstance(chunk_seq, int) or chunk_seq < 0:
                raise ValueError("Invalid chunk_seq")
            if chunk_seq < last_seq:
                raise ValueError("Sequence decreasing")
            last_seq = chunk_seq

        offset = payload.get('chunk_audio_offset_sec')
        if offset is not None:
            if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                raise ValueError("Invalid offset")
            if not (offset >= 0) or offset != offset or offset == float('inf'):
                raise ValueError("Invalid offset value")
            if offset < last_offset:
                raise ValueError("Offset decreasing")
            last_offset = offset

        audio_b64 = payload.get('audio_base64')
        if audio_b64 is not None:
            if not isinstance(audio_b64, str):
                raise ValueError("Invalid audio_base64")
            if audio_b64:
                try:
                    audio_bytes = base64.b64decode(audio_b64, validate=True)
                    audio_parts.append(audio_bytes)
                except Exception:
                    raise ValueError("Invalid base64")

        alignment = payload.get('alignment')
        if alignment is not None:
            if not isinstance(alignment, dict):
                raise ValueError("Invalid alignment")

            if chunk_seq is None or offset is None:raise ValueError('Alignment is missing its chunk origin')
            segments = alignment.get('segments',[])
            if segments is None:raise ValueError('Invalid segments')
            if segments is not None:
                if not isinstance(segments, list):
                    raise ValueError("Invalid segments")

                for seg in segments:
                    if not isinstance(seg, dict):
                        raise ValueError("Invalid segment")

                    text = seg.get('text')
                    if not isinstance(text, str):
                        raise ValueError("Invalid segment text")

                    start = seg.get('start')
                    end = seg.get('end')

                    if isinstance(start, bool) or not isinstance(start, (int, float)):
                        raise ValueError("Invalid start")
                    if isinstance(end, bool) or not isinstance(end, (int, float)):
                        raise ValueError("Invalid end")

                    if start != start or start == float('inf') or start < 0:
                        raise ValueError("Invalid start value")
                    if end != end or end == float('inf') or end < 0:
                        raise ValueError("Invalid end value")
                    if end < start:
                        raise ValueError("End before start")

            if chunk_seq is not None:
                snapshots[chunk_seq] = (offset if offset is not None else 0.0, alignment)

    if not audio_parts:
        raise ValueError("Empty audio")

    final_audio = b''.join(audio_parts)

    words = []
    for seq in sorted(snapshots.keys()):
        offset, alignment = snapshots[seq]
        segments = alignment.get('segments', [])
        for seg in segments:
            words.append({
                'word': seg['text'],
                'start': offset + seg['start'],
                'end': offset + seg['end']
            })

    import math
    if any(not math.isfinite(w['start']) or not math.isfinite(w['end']) for w in words):raise ValueError('Invalid absolute timing')

    for i in range(1, len(words)):
        if words[i]['start'] < words[i-1]['start'] or words[i]['end'] < words[i-1]['end']:
            raise ValueError("Non-monotonic words")

    return final_audio, words
