"""Portable relative library paths; supervised local Qwen draft corrected for determinism."""
from engine_limits import MAX_SEGMENTS
import hashlib,re,string,unicodedata
from pathlib import Path,PurePosixPath
ALLOWED_FIELDS={'quest_id','npc_id','dialogue_stage','segment_folder','player_value','clip_variant','segment_order','clip_id','corpus_id','dialogue_id','npc_voice_profile','clip_reference_id','clip_role','narrator_label','source_dialogue_id','source_event_id'}
RESERVED={'CON','PRN','AUX','NUL'}|{f'{base}{n}' for base in ['COM','LPT'] for n in range(1,10)}
def safe_component(value,fallback='unknown',limit=40):
 text=unicodedata.normalize('NFC','' if value is None else str(value))
 text=''.join(c for c in text if not unicodedata.category(c).startswith('C'))
 text=re.sub(r'[<>:"/\\|?*]','_',text).strip(' .') or fallback
 if text.split('.')[0].upper() in RESERVED:text='_'+text
 if len(text)>limit:text=text[:limit-9]+'_'+hashlib.sha256(text.encode()).hexdigest()[:8]
 return text
def reference_path(ident,source_path):
 """Keep original media bytes identifiable and distinct on case-insensitive disks."""
 if not isinstance(ident,str) or not ident.strip():raise ValueError('A non-empty reference ID is required.')
 suffix=Path(source_path).suffix.lower()
 # Extensionless restored media remains autodetectable; do not label it Vorbis.
 if not re.fullmatch(r'\.[a-z0-9]{1,10}',suffix):suffix='.audio'
 digest=hashlib.sha256(ident.encode('utf-8')).hexdigest()[:16]
 return 'References/'+safe_component(ident)+'__'+digest+suffix
DEFAULT_TEMPLATE='Quest Speech/{quest_id}/{segment_folder}/{player_value}/npc_{npc_id}/{dialogue_stage}/{clip_variant}/{segment_order:02d}__{clip_id}.ogg'
NARRATION_TEMPLATE='Narration/{source_dialogue_id}/{narrator_label}/{source_event_id}/{segment_order:02d}__{clip_id}.ogg'
GENERIC_TEMPLATE='Speech/{corpus_id}/{dialogue_id}/{segment_folder}/{player_value}/{clip_variant}/{segment_order:02d}__{clip_id}.ogg'
COMPLETE_FIELDS={'corpus_id','dialogue_id','dialogue_index','quest_id','npc_id','dialogue_stage','profile_id','audio_id'}
COMPLETE_TEMPLATE='Audio/{profile_id}/{dialogue_index:06d}__{dialogue_id}__{audio_id}.ogg'
def clip_path(template,fields):return _audio_path(template,fields,ALLOWED_FIELDS,'clip_id')
def complete_path(template,fields):return _audio_path(template,fields,COMPLETE_FIELDS,'audio_id')
def _audio_path(template,fields,allowed_fields,identity):
 if not isinstance(template,str) or not template or template.startswith('/') or '\\' in template:raise ValueError('Use a relative path template with forward slashes.')
 if not isinstance(fields.get(identity),str) or not fields[identity].strip():raise ValueError('A non-empty audio ID is required.')
 chunks=[];found=set()
 try:parts=list(string.Formatter().parse(template))
 except ValueError as exc:raise ValueError('Invalid path template braces.') from exc
 for literal,key,spec,conversion in parts:
  if re.search(r'[<>:"\\|?*\x00-\x1f]',literal):raise ValueError('Invalid literal character in path template.')
  chunks.append(literal)
  if key is None:continue
  if key not in allowed_fields or conversion or spec not in ({'','02d'} if key=='segment_order' else {'','06d'} if key=='dialogue_index' else {''}):raise ValueError('Unsupported template field or formatting: '+str(key))
  found.add(key);value=fields.get(key)
  if key=='segment_order':
   if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=MAX_SEGMENTS:raise ValueError(f'Segment order must be an integer from 1 to {MAX_SEGMENTS} for this runtime.')
   chunks.append(format(value,spec))
  elif key=='dialogue_index':
   if type(value) is not int or value<0:raise ValueError('Dialogue index must be a nonnegative integer.')
   chunks.append(format(value,spec))
  else:chunks.append(safe_component(value,fallback='' if key=='player_value' else 'unknown'))
 if identity not in found:raise ValueError('Include {'+identity+'} so files cannot overwrite other recordings.')
 raw=''.join(chunks)
 if '..' in raw.split('/') or raw.startswith('/') or not raw.endswith('.ogg'):raise ValueError('Paths must stay inside the library and end in .ogg.')
 result=PurePosixPath(raw)
 if result.is_absolute() or len(str(result))>240:raise ValueError('The resulting path exceeds the portable 240-character limit.')
 for part in result.parts:
  if not part.strip(' .') or part!=part.rstrip(' .') or part.split('.')[0].upper() in RESERVED:raise ValueError('Invalid literal folder or file name.')
 return result.as_posix()
