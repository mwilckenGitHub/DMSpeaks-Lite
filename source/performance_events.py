"""One source-coordinate cue resolver for NPC performance and visual narration."""
import copy,hashlib,re,json
from model import TOKEN
from stage_directions import emote_metadata,stage_format,classify_emote,EMOTE,XML_TAG
KEYS=('metadata.performance_events','performance_events','_dmspeaks_performance.events','_dmspeaks_performance_json')

def declared_key(record):
 fields=record.get('all_metadata',{})
 return next((key for key in KEYS if key in fields),None)

def events_for(record,request=None,*,ignored_spans=()):
 request=request or {};fields=record.get('all_metadata',{});key=declared_key(record);text=record['text']
 if key is None:return emote_metadata(text,ignored_spans=ignored_spans)['events'] if stage_format(record,request)=='wow' else []
 raw=fields[key]
 if key=='_dmspeaks_performance_json':
  if not isinstance(raw,str) or len(raw)>2_000_000:raise ValueError('The CSV performance metadata column must contain a JSON object.')
  try:container=json.loads(raw)
  except ValueError:raise ValueError('The CSV performance metadata column contains invalid JSON.') from None
  if not isinstance(container,dict) or container.get('source_sha256')!=hashlib.sha256(text.encode()).hexdigest():raise ValueError('Performance-event metadata belongs to earlier source text. Review its positions.')
  raw=container.get('events')
 if not isinstance(raw,list):raise ValueError('performance_events must be an array; use an empty array to explicitly disable cue inference.')
 if len(raw)>1000:raise ValueError('Use at most 1,000 performance cues per dialogue.')
 if key=='_dmspeaks_performance.events' and fields.get('_dmspeaks_performance.source_sha256')!=hashlib.sha256(text.encode()).hexdigest():raise ValueError('Performance-event metadata belongs to earlier source text. Review its positions.')
 result=[];ids=set()
 for item in raw:
  if not isinstance(item,dict):raise ValueError('Each performance event must be an object.')
  kind=item.get('kind');ident=item.get('event_id');a=item.get('source_start');b=item.get('source_end')
  if kind not in ('visual','vocal','intonation','unknown'):raise ValueError('Choose visual, vocal, intonation or unknown for each cue.')
  if not isinstance(ident,str) or not ident or ident in ids:raise ValueError('Performance event IDs must be unique nonempty strings.')
  if type(a) is not int or type(b) is not int or not 0<=a<b<=len(text):raise ValueError('Performance event offsets must be Unicode codepoints within the current source.')
  markup=text[a:b]
  if item.get('original_markup',markup)!=markup:raise ValueError('Performance event offsets refer to earlier source text.')
  if any(m.start()<b and m.end()>a and XML_TAG.fullmatch(m[1].strip()) for m in EMOTE.finditer(text)):raise ValueError('A performance cue cannot remove SSML/XML elements. Select only its text or stage annotation.')
  for token in TOKEN.finditer(text):
   if token.start()<b and token.end()>a and not a<=token.start()<token.end()<=b:raise ValueError('A performance cue cannot split a placeholder.')
  event=copy.deepcopy(item);event.update(original_markup=markup,source_start=a,source_end=b,spoken=False,speaker_role='npc' if kind in ('vocal','intonation') else 'narrator' if kind=='visual' else None)
  event['review_required']=kind=='unknown'
  if kind in ('vocal','intonation'):
   tag=event.get('tts_markup')
   if not tag:
    inferred,tag=classify_emote(markup.strip('<> '))
    if inferred!=kind:raise ValueError('Choose an explicit bracket performance control for this vocal or intonation cue.')
   if not isinstance(tag,str) or not re.fullmatch(r'\[[^\[\]\r\n]{1,160}\]',tag):raise ValueError('Use one short bracket performance control, without spoken narration.')
   event['tts_markup']=tag
  else:event['tts_markup']=''
  narration=event.get('narration',{})
  if not isinstance(narration,dict):raise ValueError('Cue narration must be an object.')
  cue=event.get('text') or narration.get('text') or markup.strip('<> ')
  if not isinstance(cue,str) or not cue.strip() or len(cue)>50000:raise ValueError('Each cue needs valid text.')
  event['narration']={**narration,'eligible':kind=='visual','text':cue if kind=='visual' else ''}
  ids.add(ident);result.append(event)
 result.sort(key=lambda e:(e['source_start'],e['source_end']))
 if any(left['source_end']>right['source_start'] for left,right in zip(result,result[1:])):raise ValueError('Performance cue source intervals must not overlap.')
 return result
