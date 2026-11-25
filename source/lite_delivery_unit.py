"""Shared full/Lite whole-recording pace preparation, before any clip cuts.

The caller supplies its approved CPU phoneme alignment function. This module
never requests speech or loads a model. Existing request identity is untouched.
"""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import subprocess

import numpy as np
import soundfile as sf

from audio import VORBIS_PAGE_OPTIONS
from vorbis_pcm import single_stream_end
from lite_portable_io import digest,file_hash,hold,read,save
from phoneme_alignment import validate,lexical_text

RATE=44100
PROFILE={'numerator':16,'denominator':15}


def prepare_unit(raw_audio,transcript,native_phones,cache,align):
    """Return a bound measured unit; reuse exact completed work on retries.

    ``align(path, identity, transcript)`` must measure the transformed audio,
    using the application's configured shared alignment lease. A busy/error
    return leaves the complete transform available and does not resubmit TTS.
    """
    raw_audio=Path(raw_audio);cache=Path(cache)
    info=sf.info(raw_audio)
    if info.samplerate!=RATE or info.channels!=1 or not 0<info.frames<=RATE*120:
        raise ValueError('The coherent pace unit needs a complete supported mono recording.')
    raw_sha=file_hash(raw_audio)
    native_identity=dict(sha256=raw_sha,sample_rate=RATE,sample_count=info.frames)
    validate(native_phones,native_identity,transcript)
    key=sha256((raw_sha+'\n'+transcript+'\n16/15').encode()).hexdigest()
    folder=cache/key;folder.mkdir(parents=True,exist_ok=True)
    with hold(folder,'.pace-unit.lock'):
        audio=folder/'coherent-16-15.wav';receipt_path=folder/'unit.json';phones_path=folder/'derived-phones.json'
        receipt=read(receipt_path) if receipt_path.is_file() else None
        if receipt is not None:
            if (receipt.get('key')!=key or receipt.get('raw_sha256')!=raw_sha
                    or receipt.get('raw_samples')!=info.frames
                    or receipt.get('transcript_sha256')!=sha256(transcript.encode()).hexdigest()
                    or receipt.get('tempo_filter')!='atempo=16/15'
                    or receipt.get('transform_scope')!='entire_raw_recording_before_any_cuts'):
                raise ValueError('The cached whole-recording pace identity changed.')
            if not audio.exists() and receipt.get('derived_discarded') is True:
                subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(raw_audio),'-af','atempo=16/15',
                    '-ar',str(RATE),'-ac','1','-n','-c:a','pcm_f32le',str(audio)],check=True,capture_output=True,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if file_hash(audio)!=receipt['derived_sha256']:
                    raise ValueError('The regenerated complete unit differs from its retained decoded clock.')
                receipt['derived_discarded']=False;save(receipt_path,receipt)
            if not audio.is_file() or file_hash(audio)!=receipt.get('derived_sha256'):
                raise ValueError('The saved whole-recording pace audio needs recovery; the original return is unchanged.')
        else:
            if audio.exists():
                raise ValueError('An unrecorded pace transform exists; inspect it before retrying.')
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(raw_audio),'-af','atempo=16/15',
                '-ar',str(RATE),'-ac','1','-n','-c:a','pcm_f32le',str(audio)],check=True,capture_output=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            transformed,rate=sf.read(audio,dtype='float32')
            if rate!=RATE or transformed.ndim!=1 or not len(transformed) or not np.isfinite(transformed).all():
                raise ValueError('The whole-recording transform has an invalid PCM clock.')
            receipt=dict(format='dmspeaks-whole-recording-unit-1',key=key,raw_sha256=raw_sha,
                raw_samples=info.frames,transcript_sha256=sha256(transcript.encode()).hexdigest(),sample_rate=RATE,
                tempo_filter='atempo=16/15',transform_scope='entire_raw_recording_before_any_cuts',
                derived_sha256=file_hash(audio),derived_samples=len(transformed),status='awaiting_measured_phones')
            save(receipt_path,receipt)
        actual=sf.info(audio)
        if (actual.samplerate,actual.channels,actual.frames)!=(RATE,1,receipt['derived_samples']):
            raise ValueError('The cached pace unit has a different sample clock.')
        identity=dict(sha256=receipt['derived_sha256'],sample_rate=RATE,sample_count=actual.frames)
        if receipt['status']=='measured_derived_phone_clock_ready':
            if not phones_path.is_file() or file_hash(phones_path)!=receipt.get('derived_phonemes_sha256'):
                raise ValueError('The saved derived phoneme evidence changed.')
            phones=read(phones_path)
        else:
            if phones_path.exists():
                raise ValueError('An unrecorded derived alignment exists; inspect it before retrying.')
            phones=align(audio,identity,transcript)
            validate(phones,identity,transcript)
            if [lexical_text(w['word']) for w in phones['words']]!=[lexical_text(w['word']) for w in native_phones['words']]:
                raise ValueError('The derived phoneme clock changed lexical ownership.')
            save(phones_path,phones)
            receipt.update(status='measured_derived_phone_clock_ready',derived_phonemes_sha256=file_hash(phones_path))
            save(receipt_path,receipt)
        validate(phones,identity,transcript)
        if file_hash(raw_audio)!=raw_sha:
            raise ValueError('The original returned recording changed during pace preparation.')
        return dict(receipt,audio=audio,phones=phones,receipt=receipt_path)


def encode_paced_segment(pcm, path):
    samples = np.ascontiguousarray(pcm, dtype='<f4').tobytes()
    run = dict(check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'f32le', '-ar', '44100',
                    '-ac', '1', '-i', 'pipe:0', '-c:a', 'libvorbis', '-q:a', '6',
                    '-fflags', '+bitexact', *VORBIS_PAGE_OPTIONS, str(path)], input=samples, **run)
    encoded = path.read_bytes()
    pcm_a, rate = sf.read(BytesIO(encoded), dtype='float32')
    pcm_b = np.frombuffer(subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path),
                          '-f', 'f32le', '-ac', '1', '-ar', '44100', '-'], **run).stdout, dtype='<f4')
    if (rate != 44100 or len(pcm_a) != len(pcm) or len(pcm_b) != len(pcm)
            or single_stream_end(path) != len(pcm) or not np.allclose(pcm_a, pcm_b, atol=1e-5, rtol=0)):
        raise ValueError('The split name clip changed when decoded.')
    return dict(sha256=digest(encoded), samples=len(pcm)), encoded, pcm_a
