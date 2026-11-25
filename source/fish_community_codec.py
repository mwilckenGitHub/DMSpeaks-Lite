"""CPU codec context worker for the local community Fish edit bridge.

Imported only inside the codec environment. Source samples remain untouched;
codec-frame quantization and estimated alignment are retained as provenance.
"""
from pathlib import Path
import hashlib
import io
import json
import math
import re
import urllib.request

from fish_community_edit import atomic_json, digest
from fish_community_tts import _extract_metadata, _validate_endpoint
from speech_edit import codec_window


def verified_bytes(path, expected):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Retained audio or codec context changed: ' + str(path))
    return raw


def resolve_capture(folder, api_id):
    """Match the pinned vLLM InputProcessor's eight-hex request suffix.

    Some stage versions retain the API ID; others expose its randomized form.
    Never use creation order or a 'newest file' heuristic. More than one match
    is ambiguous. Cached mappings avoid rescanning on local decode resume.
    """
    import numpy as np
    folder = Path(folder)
    exact = folder / (hashlib.sha256(api_id.encode()).hexdigest() + '.npz')
    candidates = [exact] if exact.is_file() else folder.glob('*.npz')
    matches = []
    for path in candidates:
        try:
            with np.load(path, allow_pickle=False) as data:
                ident = str(data['request_id'])
                belongs = ident == api_id or re.fullmatch(re.escape(api_id) + r'-[0-9a-f]{8}', ident)
                if not belongs:
                    continue
                if path.name != hashlib.sha256(ident.encode()).hexdigest() + '.npz':
                    raise ValueError('Codec capture filename differs from its identity.')
                if not bool(data['finished']):
                    raise ValueError('Matching codec capture is unfinished.')
                matches.append((path, ident))
        except (KeyError, OSError):
            continue  # unrelated diagnostic files have no request identity
    if len(matches) != 1:
        raise ValueError('Expected one codec capture for this request, found ' + str(len(matches)))
    return matches[0]


class CommunityCodecEngine:
    def __init__(self, config):
        self.config = config
        self.endpoint = _validate_endpoint(config['endpoint'])
        self.codec = None
        self.sources = {}

    def _load_codec(self):
        if self.codec is None:
            import torch
            from vllm_omni.model_executor.models.fish_speech.dac_utils import build_dac_codec
            if digest(self.config['codec_checkpoint']) != self.config['codec_sha256']:
                raise ValueError('The pinned codec checkpoint changed.')
            torch.set_num_threads(4)
            codec = build_dac_codec()
            state = torch.load(self.config['codec_checkpoint'], map_location='cpu', weights_only=True)
            codec.load_state_dict(state.get('generator', state), strict=True)
            self.codec = codec.eval().to('cpu')
        return self.codec

    def _source(self, source):
        import numpy as np
        import soundfile as sf
        import torch
        ident = source['sha256']
        if ident in self.sources:
            return self.sources[ident]
        raw = Path(source['audio_uri']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ident:
            raise ValueError('Source changed before codec encoding.')
        wave, rate = sf.read(io.BytesIO(raw), dtype='float32')
        if rate != 44100 or wave.ndim != 1 or len(wave) != source['sample_count'] or not np.isfinite(wave).all():
            raise ValueError('Source must match the declared mono 44,100 Hz samples.')
        with torch.inference_mode():
            codes, lengths = self._load_codec().encode(torch.from_numpy(wave.copy())[None, None], torch.tensor([len(wave)]))
        frames = int(lengths[0])
        if frames != math.ceil(len(wave) / 2048):
            raise ValueError('Codec frame count differs from the planned partition.')
        self.sources[ident] = codes[0, :, :frames].cpu().numpy()
        return self.sources[ident]

    def prepare(self, job, folder, seed):
        import numpy as np
        if not job['replacement_text'].strip():
            raise ValueError('Use the editor silence/skip choice for an empty replacement.')
        if 'duration_hint_samples' in job:
            raise ValueError('This bridge does not apply duration hints. Build the plan with no duration hint.')
        source = job['source']
        codes = self._source(source)
        window = codec_window(job['source_start_sample'], job['source_end_sample'],
                              source['sample_count'], source['sample_rate'], 44100, 2048, codes.shape[1])
        a, b = window['start_frame'], window['end_frame']
        if not a or b == codes.shape[1]:
            raise ValueError('The installed native adapter requires both leading and trailing audio context.')
        context = folder / 'context.npz'
        np.savez_compressed(context, prefix=codes[:, :a], suffix=codes[:, b:])
        atomic_json(folder / 'context.json', {'source_window': window, 'sha256': digest(context)})
        return {'model': self.config['model'], 'input': job['target_transcript'],
                'word_timestamps': True, 'response_format': 'wav', 'seed': seed,
                'extra_params': {'dmspeaks_infill': {
                    'prefix_codes': codes[:, :a].tolist(), 'suffix_codes': codes[:, b:].tolist()}}}

    def submit(self, payload, folder):
        import numpy as np
        import soundfile as sf
        request = urllib.request.Request(self.endpoint + '/v1/audio/speech',
                                         data=json.dumps(payload, ensure_ascii=False).encode(),
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=1800) as response:
            raw = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
        # Retain the exact returned bytes and headers even if validation fails.
        with (folder / 'returned.wav').open('xb') as stream:
            stream.write(raw)
        atomic_json(folder / 'response-headers.json', headers)
        wave, rate = sf.read(io.BytesIO(raw), dtype='float32')
        if rate != 44100 or wave.ndim != 1 or not len(wave) or not np.isfinite(wave).all():
            raise ValueError('Community response is not finite mono 44,100 Hz audio.')
        request_id = headers.get('x-dmspeaks-capture-id', '')
        if not re.fullmatch(r'speech-[A-Za-z0-9_-]+', request_id):
            raise ValueError('Runtime did not return its exact codec capture identity.')
        return {'runtime_request_id': request_id, 'headers': headers,
                'returned_sha256': digest(folder / 'returned.wav')}

    def _capture(self, folder, receipt):
        import numpy as np
        manifest = folder / 'capture.json'
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            if saved['runtime_request_id'] != receipt['runtime_request_id']:
                raise ValueError('Retained capture belongs to another response.')
            return saved, verified_bytes(folder / 'generated.npz', saved['sha256'])
        capture, request_id = resolve_capture(self.config['capture_directory'], receipt['runtime_request_id'])
        with np.load(capture, allow_pickle=False) as data:
            if str(data['request_id']) != request_id or not bool(data['finished']):
                raise ValueError('Codec capture belongs to another request or is unfinished.')
            codes = data['generated_codes']
            if codes.ndim != 2 or codes.shape[0] != 10 or not codes.shape[1] or codes.dtype.kind not in 'iu' or (codes < 0).any() or (codes[0] >= 4096).any() or (codes[1:] >= 1024).any():
                raise ValueError('Generated codec capture has invalid codes.')
            np.savez_compressed(folder / 'generated.npz', generated_codes=codes)
        saved = {'runtime_request_id': receipt['runtime_request_id'], 'capture_request_id': request_id,
                 'sha256': digest(folder / 'generated.npz')}
        atomic_json(manifest, saved)
        return saved, verified_bytes(folder / 'generated.npz', saved['sha256'])

    def finish(self, job, folder, receipt):
        import numpy as np
        import soundfile as sf
        import torch
        verified_bytes(folder / 'returned.wav', receipt['returned_sha256'])
        capture, generated_raw = self._capture(folder, receipt)
        context_info = json.loads((folder / 'context.json').read_text())
        context_raw = verified_bytes(folder / 'context.npz', context_info['sha256'])
        headers = receipt['headers']
        if headers.get('x-audio-truncated') == 'true':
            raise ValueError('Runtime reported truncated audio; retained output needs inspection.')
        with np.load(io.BytesIO(context_raw), allow_pickle=False) as context, np.load(io.BytesIO(generated_raw), allow_pickle=False) as generated:
            prefix, suffix, middle = context['prefix'], context['suffix'], generated['generated_codes']
            joined = np.concatenate([prefix, middle, suffix], axis=1)
            start = prefix.shape[1] * 2048
            end = start + middle.shape[1] * 2048
        with torch.inference_mode():
            tensor = torch.from_numpy(joined.astype(np.int64))[None]
            audio, lengths = self._load_codec().decode(tensor, torch.tensor([joined.shape[1]]))
        wave = audio[0, 0].float().cpu().numpy()
        if int(lengths[0]) < end or len(wave) < end or not np.isfinite(wave).all():
            raise ValueError('Decoded context does not cover the complete generated span.')
        valid = int(lengths[0])
        if valid > len(wave):
            raise ValueError('Decoded context length exceeds its supplied samples.')
        # Retain the same single decode for base/word ownership and timing review.
        # This is saved codec reconstruction, never another whole-sentence TTS call.
        full_context = folder / 'full-context.wav'
        full_temporary = folder / 'full-context.next.wav'
        sf.write(full_temporary, wave[:valid], 44100, subtype='PCM_24')
        full_temporary.replace(full_context)
        context_proof = {'source_window': context_info['source_window'],
                         'generated_start_sample': start, 'generated_end_sample': end,
                         'whole_frames': valid, 'generated_frames': middle.shape[1],
                         'context_preserved_in_decoder': True, 'ordinary_full_phrase_tts_used': False,
                         'new_speech_requests': 0, 'listening_approved': False,
                         'full_context_sha256': digest(full_context)}
        atomic_json(folder / 'full-context-proof.json', context_proof)
        # Only the generated middle is an insert. Context is never appended.
        output = folder / 'insert.wav'
        temporary = folder / 'insert.next.wav'
        sf.write(temporary, wave[start:end], 44100, subtype='PCM_24')
        temporary.replace(output)
        return {'output': str(output.resolve()), 'source_window': context_info['source_window'],
                'sample_rate': 44100, 'sample_count': end - start,
                'runtime_request_id': receipt['runtime_request_id'],
                'capture_request_id': capture['capture_request_id'],
                'generation_termination': 'not_reported' if 'x-audio-truncated' not in headers else 'not_truncated',
                'raw_timestamps_header': headers.get('x-word-timestamps'),
                'timestamp_source': 'estimated community attention; not verified acoustic decay',
                'decode_method': 'full prefix + generated middle + suffix codec decode, then middle samples only',
                'generated_start_sample_in_context': start, 'generated_end_sample_in_context': end,
                'full_context': {'audio_uri': str(full_context.resolve()),
                                 'sha256': context_proof['full_context_sha256'],
                                 'sample_rate': 44100, 'channels': 1, 'sample_count': valid},
                **_extract_metadata(headers)}
