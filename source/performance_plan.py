"""Compile cues without losing original text, placeholder identity or word spans."""
import copy,re
from model import parse_segments,clean_spoken,spoken_view,delimiter_source_end
from performance_events import events_for,declared_key
from stage_directions import stage_format,emote_metadata
WORDS=re.compile(r"[^\W_]+(?:['’][^\W_]+)*",re.UNICODE)

def parse_record(record,player=None,request=None):
 from delimiter_definitions import registered
 from corpus_lookup import MARKER_OUTPUTS,source_controls,cue_events
 request=request or {};text=record['text'];delimiters=registered(record);authored=declared_key(record) is not None
 controls=source_controls(record,delimiters)
 events=events_for(record,request,ignored_spans=[(start,end) for start,end,_ in controls])
 if authored:
  controls=[(start,end,rule) for start,end,rule in controls
            if not any(event['source_start']<end and event['source_end']>start for event in events)]
 ignored=[(start,end) for start,end,_ in controls]
 marker_outputs={start:MARKER_OUTPUTS.get(rule['type'],'') for start,_,rule in controls}
 parsed=parse_segments(text,player,delimiters,source_controls=controls)
 formatting=parsed.get('formatting_spans',{})
 # Authored arrays suppress inference. Unlisted WoW actions remain silent,
 # rather than passing their prose to the NPC speech service.
 conversions=copy.deepcopy(events)
 if authored and stage_format(record,request)=='wow':
  masked=list(text)
  for event in events:masked[event['source_start']:event['source_end']]=[' ']*(event['source_end']-event['source_start'])
  for event in emote_metadata(''.join(masked),ignored_spans=ignored)['events']:
   if not any(e['source_start']<event['source_end'] and e['source_end']>event['source_start'] for e in events):
    event.update(kind='unknown',tts_markup='',speaker_role=None);conversions.append(event)
 conversions=[e for e in conversions if not any(start<=e['source_start']<e['source_end']<=end for start,end in formatting.items())]
 conversions.extend(cue_events(text,controls))
 conversions.sort(key=lambda e:e['source_start'])
 if not conversions and not formatting:return parsed
 emitted=set();output_at=0;converted=[]
 for segment in parsed['segments']:
  a,b=segment['source_start'],segment['source_end'];out=[];positions=[]
  def append(value,start,end):
   out.extend(value);positions.extend([(start,end)]*len(value))
  def cue(event):
   key=bool(event.get('lookup_rule')),event['event_id']
   if key not in emitted:
    value=event['tts_markup'] or ' '
    if event['kind']=='sound_effect':
     if event['source_start'] and not text[event['source_start']-1].isspace():value=' '+value
     if event['source_end']<len(text) and not text[event['source_end']].isspace():value+=' '
    begin=output_at+len(out);append(value,event['source_start'],event['source_end']);emitted.add(key)
    converted.append({**event,'adapter_start':begin,'adapter_end':begin+len(value),'tts_markup':value})
  overlapping=[e for e in conversions if e['source_start']<b and e['source_end']>a]
  def fixed(begin,end):
   at=begin
   while at<end:
    event=next((e for e in overlapping if e['source_start']<=at<e['source_end']),None)
    if event:
     if at==event['source_start']:cue(event)
     at=min(end,event['source_end']);continue
    if at in formatting:
     append(marker_outputs.get(at,''),at,formatting[at])
     at=formatting[at];continue
    if text[at:at+2] in ('$B','$b') and at+2<=end:append('\n',at,at+2);at+=2
    else:append(text[at],at,at+1);at+=1
  if segment['type']!='fixed':
   lexical_end=delimiter_source_end(segment)
   covering=[e for e in overlapping if e['source_start']<lexical_end and e['source_end']>a]
   if covering:
    event=covering[0]
    if not event['source_start']<=a<lexical_end<=event['source_end']:raise ValueError('A performance cue cannot split a placeholder or its lexical suffix.')
    segment['speech_excluded']=True
    if event['source_start']==a:cue(event)
   else:append(segment['value']+segment.get('literal_suffix','') if segment['value'].strip() else '',a,lexical_end)
   fixed(lexical_end,b)
  else:fixed(a,b)
  resolved=''.join(out);segment['resolved_text']=resolved;segment['spoken_text']=clean_spoken(resolved)
  spoken,spans=spoken_view(resolved);mapping=[]
  for word in WORDS.finditer(spoken):
   selected=[position for lo,hi in spans[word.start():word.end()] for position in positions[lo:hi]]
   mapping.append({'word':word[0],'source_start':min(p[0] for p in selected),'source_end':max(p[1] for p in selected),'segment_order':segment['order']})
  segment['speech_word_mapping']=mapping;output_at+=len(resolved)
 if authored or formatting or any(s.get('speech_excluded') or clean_spoken(s['source_text'] if s['type']=='fixed' else s['value']+s.get('literal_suffix',''))!=s['spoken_text'] for s in parsed['segments']):
  for s in parsed['segments']:s['performance_model']='source-cues-1'
 excluded=[{k:copy.deepcopy(s[k]) for k in ('type','placeholder','source_start','source_end','source_text')} for s in parsed['segments'] if s.get('speech_excluded')]
 if excluded:
  # Variables inside an action belong to its narrator cue, not the NPC queue.
  # Collapse those silent partitions into fixed source spans. No empty name
  # clip or invented silent interval is introduced into the spoken sentence.
  merged=[]
  for segment in parsed['segments']:
   if segment.get('speech_excluded'):
    segment.update(type='fixed',placeholder='',value='');segment.pop('literal_suffix',None);segment.pop('lexical_source_end',None);segment.pop('speech_excluded',None)
   if merged and merged[-1]['type']==segment['type']=='fixed':
    prior=merged[-1];prior.update(source_end=segment['source_end'],source_text=prior['source_text']+segment['source_text'],resolved_text=prior['resolved_text']+segment['resolved_text']);prior['speech_word_mapping'].extend(segment['speech_word_mapping']);prior['spoken_text']=clean_spoken(prior['resolved_text'])
   else:merged.append(segment)
  for order,segment in enumerate(merged,1):
   segment['order']=order
   for word in segment['speech_word_mapping']:word['segment_order']=order
  parsed['segments']=merged;parsed['excluded_variables']=excluded
  parsed['warnings'].append('Variables inside silent actions belong to the optional narrator cue, not NPC speech.')
 parsed.update(resolved_text=''.join(s['resolved_text'] for s in parsed['segments']),performance_events=converted)
 parsed['spoken_text']=clean_spoken(parsed['resolved_text']);parsed['missing_values']=[s['placeholder'] for s in parsed['segments'] if s['type']!='fixed' and not s.get('speech_excluded') and not s['value'].strip()]
 return parsed

def parse_entry(entry,player=None):
 text=entry.get('active_source_text',entry.get('source_text'))
 if not isinstance(text,str):raise ValueError('The exported dialogue needs its original source text.')
 record={'text':text,'all_metadata':entry.get('source_metadata',{}),'source_text_field':entry.get('dialogue_source_text_field',entry.get('source_text_field'))}
 return parse_record(record,player,{'stage_directions':entry.get('dialogue_stage_direction_format',entry.get('stage_direction_format','auto'))})
