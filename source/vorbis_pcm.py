"""Decode Vorbis using its granule boundaries, without adding encoder padding.

FFmpeg 6 can omit final samples or expose padding for short Ogg files. Use
libsndfile for this codec so editing and delivery metadata share one clock.
Other codecs remain on the existing FFmpeg path.
"""
import array
from pathlib import Path


def is_vorbis(path):
    # The first Ogg packet is the Vorbis identification header. Inspect bytes,
    # not the extension: imported/restored reference files may have no suffix.
    with Path(path).open('rb') as source:
        header = source.read(64)
    return header.startswith(b'OggS') and b'\x01vorbis' in header


def single_stream_end(path):
    """Read the final sample clock for one complete, unchained Ogg stream."""
    with Path(path).open('rb') as source:
        size = source.seek(0, 2)
        source.seek(0)
        serial = None
        while source.tell() < size:
            page = source.read(27)
            if len(page) != 27 or page[:5] != b'OggS\x00':
                break
            flags, page_serial = page[5], page[14:18]
            if serial is None and flags & 2:
                serial = page_serial
            elif flags & 2 or page_serial != serial:
                break
            lengths = source.read(page[26])
            if len(lengths) != page[26]:
                break
            end = source.seek(sum(lengths), 1)
            if flags & 4 and end == size:
                return int.from_bytes(page[6:14], 'little', signed=True)
            if flags & 4:
                break
    raise ValueError('Use an Ogg file with one complete audio stream.')


def read_vorbis(path, *, span=None):
    return read_vorbis_source(str(path), span=span)


def read_vorbis_source(value, *, span=None):
    """Same granule-aware decoder for files or verified in-memory media."""
    import soundfile as sf

    with sf.SoundFile(value) as source:
        if source.subtype != 'VORBIS':
            raise ValueError('Expected Ogg Vorbis audio.')
        start, end = span if span is not None else (0, None)
        frame_bytes = 4 * source.channels
        pcm = array.array('f')
        position = 0
        # A decoder seek near the last Ogg page can return the right frame
        # count with different samples. Keep the sequential decoder history.
        while end is None or position < end:
            count = 65536 if end is None else min(65536, end - position)
            block = source.buffer_read(count, dtype='float32')
            if not block:
                break
            next_position = position + len(block) // frame_bytes
            if next_position > start:
                offset = max(0, start - position) * frame_bytes
                pcm.frombytes(block[offset:])
            position = next_position
        return pcm, source.samplerate, source.channels


def measure_vorbis(path):
    import soundfile as sf

    with sf.SoundFile(str(path)) as source:
        frames = 0
        while True:
            block = source.buffer_read(65536, dtype='float32')
            if not block:
                break
            frames += len(block) // (4 * source.channels)
        return frames, source.samplerate, source.channels
