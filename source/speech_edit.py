"""Provider-neutral contextual speech edit requests. No network or model loading.

Coordinates are half-open ranges in the decoded source: samples for audio and
Unicode code points for text. Candidate arrays are expanded independently, never
as a Cartesian product. Provider capability checks happen before dispatch.
"""
from __future__ import annotations
import copy, hashlib, json, re

MAX_SLOTS=32
MAX_VARIANTS_PER_SLOT=64
MAX_JOBS=1024

def _integer(value, name, minimum=0):
    if type(value) is not int or value<minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}.')
    return value

def _object(value, name, required, optional=()):
    if not isinstance(value,dict):raise ValueError(f'{name} must be an object.')
    missing=set(required)-set(value);unknown=set(value)-set(required)-set(optional)
    if missing or unknown:raise ValueError(f'{name}: missing {sorted(missing)}, unknown {sorted(unknown)}.')

def _text(value,name,empty=False):
    if not isinstance(value,str) or (not empty and not value.strip()):raise ValueError(f'{name} must be text.')
    return value

def validate_request(request):
    _object(request,'request',('format','version','operation','source','slots','policy'))
    if (request['format'],request['version'],request['operation'])!=('dmspeaks.contextual_speech_edit','0.1','replace_spans'):
        raise ValueError('Unsupported contextual speech edit format or operation.')
    source=request['source']
    _object(source,'source',('audio_uri','transcript','sample_rate','sample_count','speaker_id'),('reference_ids','sha256'))
    for field in ('audio_uri','transcript','speaker_id'):_text(source[field],'source.'+field)
    _integer(source['sample_rate'],'source.sample_rate',8000);_integer(source['sample_count'],'source.sample_count',1)
    if 'sha256' in source and (not isinstance(source['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',source['sha256'])):raise ValueError('Source sha256 must be a lowercase SHA-256 digest.')
    refs=source.get('reference_ids',[])
    if not isinstance(refs,list) or any(not isinstance(x,str) or not x.strip() for x in refs) or len(set(refs))!=len(refs):
        raise ValueError('reference_ids must be unique nonempty strings.')
    slots=request['slots']
    if not isinstance(slots,list) or not 1<=len(slots)<=MAX_SLOTS:raise ValueError('Use 1–32 replacement slots.')
    ids=set();spans=[];text_spans=[];job_count=0
    for slot in slots:
        _object(slot,'slot',('slot_id','variable_id','source_start_sample','source_end_sample','source_char_start','source_char_end','replacements'),('original_delimiter',))
        ident=_text(slot['slot_id'],'slot_id');_text(slot['variable_id'],'variable_id')
        if ident in ids:raise ValueError('Duplicate slot_id.')
        ids.add(ident)
        a=_integer(slot['source_start_sample'],'source_start_sample');b=_integer(slot['source_end_sample'],'source_end_sample')
        c=_integer(slot['source_char_start'],'source_char_start');d=_integer(slot['source_char_end'],'source_char_end')
        if not a<=b<=source['sample_count'] or not c<=d<=len(source['transcript']):raise ValueError('Edit window is outside source audio or text.')
        if a==b and c!=d:raise ValueError('A nonempty source word needs a nonempty audio window.')
        if c==d and a!=b:raise ValueError('An insertion must use a zero-width audio window.')
        spans.append((a,b));text_spans.append((c,d))
        if 'original_delimiter' in slot:_text(slot['original_delimiter'],'original_delimiter')
        variants=slot['replacements'];values=set()
        if not isinstance(variants,list) or not 1<=len(variants)<=MAX_VARIANTS_PER_SLOT:raise ValueError('Use 1–64 replacement candidates per slot.')
        for candidate in variants:
            _object(candidate,'replacement',('value_id','text'),('language','duration_hint_samples'))
            value_id=_text(candidate['value_id'],'value_id');_text(candidate['text'],'replacement.text',empty=True)
            if value_id in values:raise ValueError('Duplicate replacement value_id within a slot.')
            values.add(value_id)
            if 'language' in candidate:_text(candidate['language'],'language')
            if 'duration_hint_samples' in candidate:_integer(candidate['duration_hint_samples'],'duration_hint_samples',1)
        job_count+=len(variants)
    if job_count>MAX_JOBS:raise ValueError('This batch exceeds 1024 edits; split it into smaller requests.')
    # Require the same logical slot order in text and audio, including insertions.
    if spans!=sorted(spans) or text_spans!=sorted(text_spans):raise ValueError('Slots must be in source order.')
    for ranges in (spans,text_spans):
        if any(left[1]>right[0] or left==right for left,right in zip(ranges,ranges[1:])):raise ValueError('Replacement slots overlap or duplicate a position.')
    policy=request['policy']
    _object(policy,'policy',('require_contextual_infilling','allow_duration_change','missing_replacement','preserve_original_assets'),('performance_targets','context_encoding'))
    if policy['require_contextual_infilling'] is not True or policy['preserve_original_assets'] is not True:raise ValueError('This operation requires native contextual infilling and preserved source assets.')
    if type(policy['allow_duration_change']) is not bool:raise ValueError('allow_duration_change must be boolean.')
    if policy['missing_replacement'] not in ('silence','skip','error'):raise ValueError('Unknown missing replacement policy.')
    if policy.get('context_encoding','global_codec') not in ('global_codec','independent_crops_experimental'):raise ValueError('Unknown contextual audio encoding mode.')
    targets=policy.get('performance_targets',[])
    if not isinstance(targets,list) or any(x not in ('voice_identity','emotion','cadence','intonation','loudness') for x in targets) or len(set(targets))!=len(targets):raise ValueError('Invalid performance targets.')
    return copy.deepcopy(request)

def plan_edits(request, capabilities):
    """Expand one independently contextualized job per slot value; never fallback."""
    data=validate_request(request)
    if not isinstance(capabilities,dict) or capabilities.get('contextual_infilling') is not True:
        raise ValueError('Selected provider does not advertise contextual infilling. No ordinary TTS fallback was used.')
    encoding=data['policy'].get('context_encoding','global_codec')
    encodings=capabilities.get('context_encodings',[])
    if encoding!='global_codec' and (not isinstance(encodings,list) or encoding not in encodings):raise ValueError('Selected provider does not advertise the requested experimental context encoding.')
    if not data['policy']['allow_duration_change'] and capabilities.get('exact_duration') is not True:
        raise ValueError('Selected provider cannot guarantee an unchanged duration. A duration hint is not a guarantee.')
    source=data['source'];jobs=[]
    for slot in data['slots']:
        for candidate in slot['replacements']:
            target=source['transcript'][:slot['source_char_start']]+candidate['text']+source['transcript'][slot['source_char_end']:]
            job={'slot_id':slot['slot_id'],'variable_id':slot['variable_id'],'value_id':candidate['value_id'],'replacement_text':candidate['text'],'target_transcript':target,'source':copy.deepcopy(source),'source_start_sample':slot['source_start_sample'],'source_end_sample':slot['source_end_sample'],'source_char_start':slot['source_char_start'],'source_char_end':slot['source_char_end'],'policy':copy.deepcopy(data['policy'])}
            for field in ('language','duration_hint_samples'):
                if field in candidate:job[field]=candidate[field]
            # Provider/model/version must extend this identity before caching audio.
            job['request_hash']=hashlib.sha256(json.dumps(job,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            jobs.append(job)
    return jobs

def codec_window(start_sample,end_sample,source_samples,source_rate,codec_rate,hop_samples,frame_count):
    """Enclose a source window in codec frames; expose quantization explicitly."""
    for value,name in ((source_samples,'source_samples'),(source_rate,'source_rate'),(codec_rate,'codec_rate'),(hop_samples,'hop_samples'),(frame_count,'frame_count')):_integer(value,name,1)
    _integer(start_sample,'start_sample');_integer(end_sample,'end_sample')
    if not start_sample<=end_sample<=source_samples:raise ValueError('Source window is outside decoded audio.')
    scale=codec_rate/source_rate
    denominator=source_rate*hop_samples
    first=min(frame_count,(start_sample*codec_rate)//denominator)
    last=first if start_sample==end_sample else min(frame_count,(end_sample*codec_rate+denominator-1)//denominator)
    if first==0 and last==frame_count:raise ValueError('Infilling needs audio context outside the replacement window.')
    return {'start_frame':first,'end_frame':last,'source_start_sample':start_sample,'source_end_sample':end_sample,'quantized_start_sample':round(first*hop_samples/scale),'quantized_end_sample':min(source_samples,round(last*hop_samples/scale)),'codec_rate':codec_rate,'hop_samples':hop_samples,'timing_source':'codec_frame_quantization'}
