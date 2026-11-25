"""Build contextual replacement arrays from a corpus and one aligned full take."""
from __future__ import annotations
from engine_limits import MAX_SEGMENTS
from performance_plan import parse_record
import copy,hashlib,json,math,re
from pathlib import Path
from model import parse_segments,clean_spoken,spoken_view
from delimiter_definitions import registered
from fish_community_tts import has_attention_header_clock, restore_attention_frame_time
from spoken_spans import normalized_spoken_spans
from speech_edit import validate_request

WORDS=re.compile(r"[^\W_]+(?:['’][^\W_]+)*",re.UNICODE)
def _normal(word):return word.replace('’',"'").casefold()

def timing_label_matches(word, label):
    """Compare the single spoken word, allowing punctuation around a provider label."""
    parts = WORDS.findall(label)
    return len(parts) == 1 and _normal(parts[0]) == _normal(word)

def delimiter_text_spans(segments, transcript, spans):
    """Locate every spoken or silent delimiter on the normalized transcript."""
    offset = 0
    for segment in segments:
        start = offset
        offset += len(segment['resolved_text'])
        if segment['type'] == 'fixed' or segment.get('speech_excluded'):
            continue
        value = segment['value']
        end = start + (len(value) + len(segment.get('literal_suffix', '')) if value.strip() else 0)
        indices = [index for index, (left, right) in enumerate(spans)
                   if left < end and right > start and not transcript[index].isspace()]
        if indices:
            first, last = indices[0], indices[-1] + 1
        else:
            first = next((index for index, (left, _) in enumerate(spans) if left >= start), len(transcript))
            last = first
        yield segment, start, end, first, last


def build_request(record,source_asset,domains,duration_hint_policy='none',*,target=None,requested_orders=None):
    """Read-only construction; caller supplies a stable mono source audio asset.

    Returned bindings retain all placeholder occurrences, including ones whose
    variants were not requested, so a later library export cannot embed a name
    or class into a supposedly fixed segment.
    """
    if duration_hint_policy not in ('none','source_span'):raise ValueError('Choose no duration hint or the original source-span duration.')
    if source_asset.get('record_id')!=record['dataset']+':'+str(record['index']):raise ValueError('Choose a full take belonging to the selected source dialogue.')
    if source_asset.get('source_revision')!=record['revision'] or source_asset.get('source_metadata_revision')!=record.get('metadata_revision'):raise ValueError('The source text or metadata changed after this take. Review or regenerate its baseline before infilling.')
    generation=source_asset.get('generation',{})
    if generation.get('mode')!='context':raise ValueError('Contextual editing needs a full dialogue take, not an isolated or assembled replacement.')
    if source_asset.get('channels',1)!=1:raise ValueError('Prepare a mono context source for the current native infilling adapter.')
    rate=source_asset.get('sample_rate');count=source_asset.get('sample_count')
    if type(rate) is not int or rate<=0 or type(count) is not int or count<=0:raise ValueError('The full take needs measured decoded sample coordinates.')
    player=copy.deepcopy(source_asset.get('player',{}));parsed=parse_record(record,player,{'stage_directions':source_asset.get('generation',{}).get('stage_direction_format','auto')})
    if len(parsed['segments'])>MAX_SEGMENTS or any('unfinished' in w.casefold() for w in parsed['warnings']):raise ValueError(f'Resolve incomplete source markup or more than {MAX_SEGMENTS} segments before infilling. The editing adapter may separately limit replacement slots per request.')
    if not isinstance(domains,dict) or not domains:raise ValueError('Choose one or more variable candidate arrays.')
    occurrences={s['order']:s for s in parsed['segments'] if s['type']!='fixed' and not s.get('speech_excluded')}
    available={s['type'] for s in occurrences.values()}
    if set(domains)-available:raise ValueError('Candidate arrays include variables that do not occur in this dialogue.')
    target_order=None
    selected_orders=None
    if requested_orders is not None:
        if (target is not None or not isinstance(requested_orders,list) or not requested_orders
                or any(type(order) is not int or order not in occurrences for order in requested_orders)
                or len(set(requested_orders))!=len(requested_orders)):
            raise ValueError('Choose distinct spoken replacement occurrences in this dialogue.')
        selected_orders=set(requested_orders)
        if not set(domains)<={occurrences[order]['type'] for order in selected_orders}:
            raise ValueError('A replacement array does not match any requested occurrence.')
    if target is not None:
        if (not isinstance(target,dict) or set(target)!={'order','type','placeholder'}
                or type(target['order']) is not int or not isinstance(target['type'],str)
                or not isinstance(target['placeholder'],str)):
            raise ValueError('Choose one replacement occurrence.')
        selected=next((s for s in parsed['segments'] if s['order']==target['order']),None)
        if (selected is None or selected['type']=='fixed' or selected.get('speech_excluded')
                or selected['type']!=target['type'] or selected['placeholder']!=target['placeholder']
                or set(domains)!={selected['type']}):
            raise ValueError('The selected replacement occurrence changed. Open a new plan.')
        target_order=selected['order']
    for kind,values in domains.items():
        if not isinstance(values,list) or not 1<=len(values)<=64:raise ValueError('Use 1–64 values in each replacement array.')
        encoded=[]
        for value in values:
            if not isinstance(value,(str,int,float,bool)) or isinstance(value,float) and not math.isfinite(value):raise ValueError('Replacement values must be finite JSON strings, numbers or booleans.')
            encoded.append(json.dumps(value,sort_keys=True,ensure_ascii=False))
        if len(set(encoded))!=len(encoded):raise ValueError('Duplicate replacement values in a candidate array.')
    resolved=''.join(s['resolved_text'] for s in parsed['segments']);transcript,spans=normalized_spoken_spans(resolved)
    if transcript!=generation.get('spoken_text'):raise ValueError('Baseline transcript differs from the selected source and its original test inputs.')
    tokens=list(WORDS.finditer(transcript));words=source_asset.get('words',[])
    if len(words)!=len(tokens) or not words:raise ValueError('The full take needs word timings matching its transcript.')
    previous_start=previous_end=-1
    for token,word in zip(tokens,words):
        if not timing_label_matches(token[0], str(word.get('word', ''))):
            raise ValueError('Word timings refer to a different transcript.')
        start,end=word.get('start'),word.get('end')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or start<0 or end<start or start<previous_start or end<previous_end or end>count/rate+1/rate:raise ValueError('Word timings must be ordered and within the complete source recording.')
        previous_start,previous_end=start,end
    if has_attention_header_clock(source_asset):
        # The provider serializes frames as whole milliseconds, which can move
        # a cut almost a millisecond early and expand it by an entire frame.
        words = [{**word, **{
            edge: min(count / rate, restore_attention_frame_time(word[edge]))
            for edge in ('start', 'end')
        }} for word in words]
    delimiters = registered(record)
    bindings=[];slots=[]
    for segment, raw_start, raw_end, c, d in delimiter_text_spans(parsed['segments'], transcript, spans):
        literal=segment.get('literal_suffix','')
        if c<d:
            indices=[i for i,t in enumerate(tokens) if t.start()<d and t.end()>c]
            if not indices or tokens[indices[0]].start()<c or tokens[indices[-1]].end()>d:raise ValueError('A placeholder shares a word with fixed text. Expand its edit window before deriving independent clips.')
            a=max(0,round(words[indices[0]]['start']*rate));b=min(count,round(words[indices[-1]]['end']*rate))
            if b<=a:raise ValueError('The placeholder has no usable audio interval.')
            previous_end=round(words[indices[0]-1]['end']*rate) if indices[0] else 0
            next_start=round(words[indices[-1]+1]['start']*rate) if indices[-1]+1<len(words) else count
        else:
            before=[i for i,t in enumerate(tokens) if t.end()<=c];after=[i for i,t in enumerate(tokens) if t.start()>=c]
            left=words[before[-1]]['end'] if before else 0;right=words[after[0]]['start'] if after else count/rate
            a=b=max(0,min(count,round((left+right)/2*rate)))
            previous_end=round(left*rate);next_start=round(right*rate)
        kind=segment['type'];legacy=bool(re.match(r'^\$[nNcCrRgG]',segment['placeholder']))
        binding={'slot_id':'segment-'+str(segment['order']),'slot_order':segment['order'],'variable_id':('player.' if legacy else 'variables.')+kind,'kind':kind,'original_delimiter':segment['placeholder'],'source_char_start':c,'source_char_end':d,'source_start_sample':a,'source_end_sample':b,'source_value':segment['value'],'source_spoken_text':clean_spoken(segment['value']+literal) if segment['value'].strip() else '','replacement_values':{},'timing_source':source_asset.get('timing_source','unavailable')}
        bindings.append(binding)
        binding.update(preceding_word_end_sample=previous_end,following_word_start_sample=next_start)
        if kind not in domains or (target_order is not None and segment['order']!=target_order):continue
        if selected_orders is not None and segment['order'] not in selected_orders:continue
        replacements=[]
        for value in domains[kind]:
            replacement_player = {**player, kind: value}
            # Use the same project marker mapping as the baseline parser. A
            # custom marker such as #City has no built-in token_value mapping.
            replacement = parse_segments(segment['placeholder'], replacement_player, delimiters)
            rendered = next((part['value'] for part in replacement['segments'] if part['type'] == kind), '')
            if not clean_spoken(rendered):raise ValueError('This array includes an empty spoken replacement. Use the missing-clip policy or an explicit deletion workflow.')
            text=clean_spoken(rendered+literal)
            if c==d:
                # Normalization can collapse both spaces around a missing value
                # into the same source character. Restore only separators that
                # were present around the placeholder, not neighboring speech.
                before=spoken_view(resolved[:raw_start])[0];after=spoken_view(resolved[raw_end:])[0]
                if before and before[-1].isspace() and c and not transcript[c-1].isspace():text=' '+text
                if after and after[0].isspace() and c<len(transcript) and not transcript[c].isspace():text+=' '
            ident=type(value).__name__+':'+json.dumps(value,ensure_ascii=False,separators=(',',':'))
            binding['replacement_values'][ident]=rendered
            candidate={'value_id':ident,'text':text}
            if duration_hint_policy=='source_span' and b>a:candidate['duration_hint_samples']=b-a
            language=record.get('metadata',{}).get('dialogue_language')
            if language:candidate['language']=language
            replacements.append(candidate)
        slots.append({key:binding[key] for key in ('slot_id','variable_id','original_delimiter','source_char_start','source_char_end','source_start_sample','source_end_sample')}|{'replacements':replacements})
    file=Path(source_asset['path']).resolve()
    if not file.is_file():raise ValueError('The source recording is no longer available.')
    digest=hashlib.sha256(file.read_bytes()).hexdigest()
    if source_asset.get('sha256') and source_asset['sha256']!=digest:raise ValueError('The source recording changed after alignment.')
    speaker=record.get('speaker',{}).get('id') or record.get('metadata',{}).get('npc_id') or record['id']
    request={'format':'dmspeaks.contextual_speech_edit','version':'0.1','operation':'replace_spans','source':{'audio_uri':str(file),'transcript':transcript,'sample_rate':rate,'sample_count':count,'sha256':digest,'speaker_id':str(speaker)},'slots':slots,'policy':{'require_contextual_infilling':True,'allow_duration_change':True,'missing_replacement':'silence','preserve_original_assets':True,'performance_targets':['voice_identity','emotion','cadence','intonation','loudness']}}
    validate_request(request)
    return {'request':request,'bindings':bindings,'source_asset_id':source_asset['id'],'source_revision':record['revision'],'source_metadata_revision':record.get('metadata_revision'),'notice':'Word boundaries are estimated unless verified. Native codec windows may be quantized; exports must use the returned audio-window coordinates and reject conflicting overlaps.'}

def prepare_lossless_source(app,asset):
    """Cache a lossless context decode once; never change the original take."""
    from audio import decode,encode_preview_pcm
    file=Path(asset['path']);digest=hashlib.sha256(file.read_bytes()).hexdigest()
    if asset.get('sha256') and asset['sha256']!=digest:raise ValueError('Source audio changed after alignment.')
    if asset.get('channels',1)!=1:raise ValueError('Prepare a mono source for the current native infilling adapter.')
    pcm=decode(file,sr=asset['sample_rate'],channels=1)
    if len(pcm)!=asset.get('sample_count'):raise ValueError('Source decode no longer matches its recorded sample count.')
    target=app.folder/'edit-sources'/(digest+'.flac');metadata=target.with_suffix('.json')
    if target.is_file():
        if not metadata.is_file():raise ValueError('Cached context source has no integrity record; prepare a new source cache.')
        saved=json.loads(metadata.read_text())
        if saved.get('sha256')!=hashlib.sha256(target.read_bytes()).hexdigest():raise ValueError('Cached context source changed after preparation.')
    else:
        encode_preview_pcm(pcm,target,sr=asset['sample_rate'])
        metadata.write_text(json.dumps({'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'source_sha256':digest,'sample_rate':asset['sample_rate'],'sample_count':len(pcm)},indent=2))
    return {**copy.deepcopy(asset),'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
