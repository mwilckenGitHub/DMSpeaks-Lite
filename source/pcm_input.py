"""Reviewed local Qwen helper: close PCM writers before external readers open them."""
from contextlib import contextmanager
import array
import pathlib
import tempfile

@contextmanager
def pcm_input_file(pcm, padding_samples=0):
    if isinstance(padding_samples, bool) or not isinstance(padding_samples, int) or padding_samples < 0:
        raise ValueError("padding_samples must be a non-negative integer")
    if not isinstance(pcm, array.array) or pcm.typecode != 'f':
        raise ValueError("pcm must be an array.array with typecode 'f'")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = pathlib.Path(tmpdir) / "input.pcm"
        with open(path, "wb") as f:
            f.write(pcm.tobytes())
            if padding_samples:
                f.write((array.array('f', [0.0]) * padding_samples).tobytes())
        yield path
