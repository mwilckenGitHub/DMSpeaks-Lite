"""Local optional alignment backends. Provider timestamps take precedence in TTS.

Configuration lives in [alignment] in the connection INI. This module never
installs packages, downloads weights or calls a paid speech service.
"""
import hashlib,json,os,re,subprocess,uuid
from pathlib import Path

ENGINES={'auto','qwen_local','ctc_local','provider_only'}

def source_alignment_fingerprint(asset):
    coordinates={'sample_rate':asset.get('sample_rate'),'sample_count':asset.get('sample_count'),'timing_source':asset.get('timing_source'),'words':[{k:w.get(k) for k in ('word','start','end')} for w in asset.get('words',[])]}
    return hashlib.sha256(json.dumps(coordinates,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def validate_settings(settings):
    if not isinstance(settings,dict) or set(settings)-{'engine','python','model_path','device','language','timeout'}:
        raise ValueError('Unknown alignment settings. Use engine, python, model_path, device, language and timeout.')
    if any(not isinstance(v,str) or '\n' in v or '\r' in v for v in settings.values()):raise ValueError('Alignment settings must be single-line text.')
    candidate={'engine':'auto','python':'','model_path':'','device':'cpu','language':'English','timeout':'300',**settings}
    if candidate['engine'] not in ENGINES:raise ValueError('Choose auto, qwen_local, ctc_local or provider_only for alignment.')
    if candidate['device'] not in {'cpu','cuda:0'}:raise ValueError('Local alignment device must be cpu or cuda:0.')
    try:timeout=int(candidate['timeout'])
    except (ValueError,TypeError):raise ValueError('Alignment timeout must be 30–900 seconds.') from None
    if not 30<=timeout<=900:raise ValueError('Alignment timeout must be 30–900 seconds.')
    if not 1<=len(candidate['language'])<=64 or any(ord(c)<32 for c in candidate['language']):raise ValueError('Choose an alignment language name or code.')
    if any('://' in candidate[k] for k in ['python','model_path']):raise ValueError('Local alignment needs local paths. Use speech-provider timestamps for hosted services.')
    return candidate

def select_backend(root,lab,config=None,language=None):
    settings=validate_settings(dict(config or {}));engine=settings['engine']
    device=settings['device'];timeout=int(settings['timeout'])
    requested_language=language
    language_source='corpus' if isinstance(language,str) and language.strip() and language.strip().casefold()!='und' else 'configured_alignment_default'
    language=(settings.get('language') or 'English') if language_source=='configured_alignment_default' else language
    if not isinstance(language,str) or not 1<=len(language)<=64 or any(ord(c)<32 for c in language):raise ValueError('Choose an alignment language name or code.')
    language=language.strip()
    if re.fullmatch(r'[a-zA-Z]{2,3}[-_][a-zA-Z0-9-]+',language):language=re.split('[-_]',language)[0]
    elif re.fullmatch(r'[a-z]{2}[A-Z]{2}',language):language=language[:2]
    language_info={'language_source':language_source,'requested_language':requested_language}
    root,lab=Path(root),Path(lab)
    def configured_path(key,default):
        value=settings.get(key) or str(default)
        if '://' in value:raise ValueError('Local alignment needs local paths. Use speech-provider timestamps for hosted services.')
        path=Path(value).expanduser();return path if path.is_absolute() else root/path
    model=configured_path('model_path',lab/'models/qwen3-forced-aligner-0.6b-hf')
    qwen_python=configured_path('python',lab/'tools/fish-speech/.venv/bin/python')
    if engine=='provider_only':raise ValueError('This project uses provider timestamps only. Choose a service returning word times or configure a local aligner; the audio is retained.')
    if engine=='qwen_local' or engine=='auto' and (model/'model.safetensors').is_file() and qwen_python.is_file():
        if not qwen_python.is_file() or not (model/'model.safetensors').is_file():raise ValueError('Configure a local Qwen alignment interpreter and an installed model; no weights were downloaded.')
        return {'engine':'qwen_local','python':qwen_python,'worker':root/'companions/qwen_align_worker.py','model':model,'device':device,'language':language,'timeout':timeout,**language_info}
    python=configured_path('python',lab/'tools/index-tts/.venv/bin/python')
    if not python.is_file():raise ValueError('No local forced aligner is configured. Provider audio is retained; word timestamps are unavailable.')
    if language.casefold() not in {'en','english'}:raise ValueError('The cached CTC aligner is English-only. Configure Qwen alignment or use provider word timestamps for this language.')
    return {'engine':'ctc_local','python':python,'worker':root/'align_worker.py','language':language,'timeout':timeout,**language_info}

def align(root,lab,folder,path,text,config=None,language=None):
    backend=select_backend(root,lab,config,language)
    if backend['engine']=='ctc_local' and (re.search(r'[^A-Za-z\s\W_]',text) or any(c.isalpha() and not c.isascii() for c in text)):
        raise ValueError('Cached CTC alignment supports English letters only. Configure Qwen alignment or supply provider word timestamps for numerals and other scripts.')
    ident=uuid.uuid4().hex;folder=Path(folder);transcript=folder/(ident+'.transcript');output=folder/(ident+'.alignment.json');transcript.write_text(text,encoding='utf-8')
    try:
        args=[str(backend['python']),str(backend['worker']),str(path),str(transcript),str(output)]
        if backend['engine']=='qwen_local':args+=['--model',str(backend['model']),'--device',backend['device'],'--language',backend['language']]
        try:result=subprocess.run(args,capture_output=True,timeout=backend['timeout'],env={**os.environ,'DIALOGUE_STUDIO_LAB':str(lab)},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:raise ValueError('Local alignment timed out. The full audio remains available; no partial timings were applied.') from None
        except OSError:raise ValueError('The configured local alignment interpreter could not start. Check its path and environment; no timings were applied.') from None
        if result.returncode:
            diagnostic=folder/(ident+'.alignment-error.log');diagnostic.write_bytes(result.stderr[-16000:])
            raise ValueError('Local '+backend['engine']+' alignment failed. The full audio is retained. Diagnostic: '+str(diagnostic))
        value=json.loads(output.read_text(encoding='utf-8'))
        if not isinstance(value,dict) or not isinstance(value.get('words'),list) or not str(value.get('timing_source','')).startswith('estimated'):raise ValueError('The local aligner returned an invalid timing document.')
        return {**value,'backend':backend['engine'],'language_source':backend['language_source'],'requested_language':backend['requested_language']}
    finally:transcript.unlink(missing_ok=True);output.unlink(missing_ok=True)

class TimingCoordinatesError(ValueError):
    """The transcript matches, but its timestamps cannot be used."""


def validate_words(words,expected,duration):
    """Validate coordinates and transcript identity; never a speech-content check."""
    import math
    if not isinstance(words,list) or len(words)!=len(expected):raise ValueError('Returned word count differs from the source; full audio retained without unsafe splitting.')
    normalize=lambda word:re.sub(r'\W','',word).casefold()
    for word,source in zip(words,expected):
        if not isinstance(word,dict) or not isinstance(word.get('word'),str):raise ValueError('The aligner returned an invalid word record.')
        if normalize(word['word'])!=normalize(source['word']):raise ValueError('Timestamp transcript differs from source; full audio retained without unsafe splitting.')
    previous_start=previous_end=-1
    for word in words:
        a,b=word.get('start'),word.get('end')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)) or a<0 or b<a or a<previous_start or b<previous_end or b>duration+1/44100:
            raise TimingCoordinatesError('Word timings are outside the audio or not ordered; keeping full audio without unsafe splits.')
        previous_start,previous_end=a,b


def bind_word_times(words, expected, duration):
    """Attach source spans after verifying the complete timed transcript."""
    validate_words(words, expected, duration)
    return [{**word, **{key: value for key, value in span.items() if key != 'word'}}
            for word, span in zip(words, expected)]
