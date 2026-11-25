"""Dialogue-owned delimiter definitions, saved as reversible corpus metadata."""
import copy,json,re,unicodedata

FIELD='_dmspeaks_delimiters'
FILE_FORMAT='dmspeaks-delimiters'
LIMIT=256
KEY=re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,63}\Z')
NAME=re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
BRACKET_CONTROL=re.compile(r'\[\[[^\[\]\r\n]+\]\]|\[[^\[\]\r\n]+\]')
DISPLAY_BRACKET=re.compile(r'\[/?(?:b|i|u|s|bold|italic|underline|strike)\]',re.IGNORECASE)
DISPLAY_XML=re.compile(r'</?(?:b|bold|strong|i|italic|u|underline|strike)>',re.IGNORECASE)
COMMENT_REVISION_ANCHOR='_audio_revision_notes'
CHOICE_REVISION_ANCHOR='_audio_revision_choices'
INFERRED_COMMENTS_FIELD='_dmspeaks_inferred_delimiter_comments'
XML_CONTROL=re.compile(r'</?[A-Za-z_][\w:.-]*(?=[\s/>])[^<>]*>|<!--.*?-->|<\?.*?\?>')
XML_ENTITY=re.compile(r'&(?:#(?:x[0-9A-Fa-f]+|[0-9]+)|[A-Za-z][A-Za-z0-9]+);')

def validate_definition(value):
 if not isinstance(value,dict):raise ValueError('Enter a delimiter definition.')
 result={key:value.get(key,default) for key,default in [('key',''),('name',''),('representation',''),('notes',''),('choices',[]),('default',''),('color_mode','auto'),('color','#7DD3FC')]}
 kind=value.get('type','replacement')
 if kind not in ('replacement','formatting'):raise ValueError('Choose Replacement or Formatting as the delimiter type.')
 if kind=='formatting':result['type']=kind
 if 'snap_mode' in value:
  from replacement_snap import FAMILIES
  snap=value['snap_mode']
  if kind!='replacement' or not isinstance(snap,str) or snap not in ('off',*FAMILIES):
   raise ValueError('Choose a valid default snapping mode for this replacement delimiter.')
  result['snap_mode']=snap
 for key,limit in [('key',64),('name',200),('representation',200),('notes',4000),('default',1000)]:
  if not isinstance(result[key],str) or len(result[key])>limit:raise ValueError('Invalid delimiter '+key+'.')
 if COMMENT_REVISION_ANCHOR in value:
  anchor=value[COMMENT_REVISION_ANCHOR]
  if not isinstance(anchor,str) or len(anchor)>4000:raise ValueError('Invalid delimiter audio revision notes.')
  result[COMMENT_REVISION_ANCHOR]=anchor
 if CHOICE_REVISION_ANCHOR in value:
  anchor=value[CHOICE_REVISION_ANCHOR]
  if anchor is not None and (not isinstance(anchor,list) or len(anchor)>1000 or
     any(not isinstance(item,str) or not item.strip() or len(item)>1000 for item in anchor)):
   raise ValueError('Invalid delimiter audio revision choices.')
  result[CHOICE_REVISION_ANCHOR]=anchor
 if not KEY.fullmatch(result['key']) or result['key'] in {'fixed','__proto__','prototype','constructor'}:raise ValueError('Use a variable key beginning with a letter or underscore, followed by letters, numbers, underscores or hyphens.')
 if not result['name'].strip():raise ValueError('Enter a delimiter name.')
 if result['color_mode'] not in {'auto','manual'} or not isinstance(result['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',result['color']):raise ValueError('Choose automatic color or a valid custom color.')
 result['color']=result['color'].upper()
 marker=result['representation']
 if not marker or marker!=marker.strip() or not any(c.isprintable() and not c.isspace() for c in marker) or any(unicodedata.category(c) in {'Cc','Cs','Zl','Zp'} for c in marker):
  raise ValueError('Enter a visible delimiter string without leading or trailing spaces, line breaks, or control characters.')
 from model import TOKEN,token_value
 builtin_key=token_value(marker,{})[0] if TOKEN.fullmatch(marker) else None
 if builtin_key and KEY.fullmatch(builtin_key) and builtin_key!=result['key']:
  raise ValueError('This built-in delimiter string already identifies a different variable.')
 if marker in {'$B','$b'}:raise ValueError('$B is a paragraph control, not a replacement delimiter.')
 choices=result['choices']
 if not isinstance(choices,list) or len(choices)>1000 or any(not isinstance(c,str) or not c.strip() or len(c)>1000 for c in choices):raise ValueError('Enter up to 1,000 nonempty replacement values.')
 result['choices']=list(dict.fromkeys(choices));return result

def check_provider_marker(definition, profile):
 if not profile:return
 marker=definition['representation']
 syntax=profile.get('syntax')
 capabilities=profile.get('capabilities','')
 if isinstance(capabilities,str):capabilities=capabilities.split(',')
 caps={item.strip().lower() for item in capabilities}
 if not syntax:
  syntax='ssml' if 'ssml' in caps else 'brackets' if 'brackets' in caps else 'plain'
 label=profile.get('label') or profile.get('id') or 'this connection'
 if syntax in {'brackets','instructions'} and any(not match[0].startswith('[[') for match in BRACKET_CONTROL.finditer(marker)):
  # Explicit display tags are stripped before speech. A configured direction
  # with the same spelling still belongs to the active connection.
  if definition.get('type')=='formatting' and DISPLAY_BRACKET.fullmatch(marker):
   directions=profile.get('direction_tags') or []
   if isinstance(directions,str):directions=json.loads(directions)
   if marker[1:-1].casefold() not in {direction.casefold() for direction in directions}:return
  hint=' Select Formatting for a display tag such as [bold].' if DISPLAY_BRACKET.fullmatch(marker) else ''
  raise ValueError(f'{marker} uses bracket syntax reserved for speech controls by {label}. Choose a different marker.{hint}')
 if syntax=='ssml' and XML_CONTROL.search(marker):
  if definition.get('type')=='formatting' and DISPLAY_XML.fullmatch(marker):return
  raise ValueError(f'{marker} uses SSML syntax reserved by {label}. Choose a different marker.')

def check_authoring_marker(definition, profile, record):
 from model import TOKEN
 from stage_directions import EMOTE, stage_format
 marker=definition['representation']
 builtin=TOKEN.match(marker)
 if re.match(r'\$[bB]',marker) or XML_ENTITY.search(marker):
  raise ValueError(f'{marker} uses reserved source formatting syntax. Choose a different marker.')
 if builtin and re.match(r'\$[nNcCrRgG]',marker):
  if builtin.end()!=len(marker) or definition.get('type')=='formatting':
   raise ValueError(f'{builtin[0]} is reserved for a built-in replacement. Choose a different marker.')
 if DISPLAY_XML.fullmatch(marker):
  if definition.get('type')!='formatting':raise ValueError(f'{marker} is reserved for display formatting. Select Formatting.')
 elif EMOTE.fullmatch(marker) and stage_format(record,{})=='wow':
  raise ValueError(f'{marker} uses angle brackets reserved for WoW source cues. Choose a different marker.')
 check_provider_marker(definition,profile)

def check_provider_delimiters(record,profile):
 from model import delimiter_pattern
 definitions=registered(record)
 if not definitions:return
 markers={item['representation']:item for item in definitions}
 used={match[0] for match in delimiter_pattern(definitions).finditer(record['text'])}
 for marker in sorted(markers.keys() & used):
  check_provider_marker(markers[marker],profile)

def local_registered(record):
 flat=record.get('all_metadata',{})
 value=flat.get(FIELD,flat.get(FIELD+'_json',[]))
 if isinstance(value,str):
  try:value=json.loads(value)
  except ValueError as exc:raise ValueError('Invalid saved delimiter definitions.') from exc
 if not isinstance(value,list) or len(value)>LIMIT:raise ValueError('Invalid saved delimiter definitions.')
 result=[validate_definition(d) for d in value]
 if len({d['key'] for d in result})!=len(result) or len({d['representation'] for d in result})!=len(result):raise ValueError('Delimiter keys and strings must be unique.')
 return result

def registered(record):
 from project_delimiter_resolution import resolve
 return resolve(record.get('project_delimiters', []), local_registered(record))

def inferred_comments(fields):
 raw=fields.get(INFERRED_COMMENTS_FIELD)
 if not isinstance(raw,str):return {}
 try:comments=json.loads(raw)
 except ValueError:return {}
 if not isinstance(comments,dict) or len(comments)>LIMIT:return {}
 return {key:note for key,note in comments.items() if isinstance(note,str) and len(note)<=4000}

def definitions_for_record(record):
 from model import TOKEN,delimiter_pattern,token_value
 flat=record.get('all_metadata',{});found={}
 for key,value in flat.items():
  if key.startswith('variables.'):
   parts=key.split('.')
   if len(parts)==3:found.setdefault(parts[1],{})[parts[2]]=value
 markers={}
 authored=registered(record)
 authored_markers={d['representation'] for d in authored}
 for match in delimiter_pattern(authored).finditer(record.get('text','')):
  if match[0] in authored_markers or not TOKEN.fullmatch(match[0]):continue
  key,_=token_value(match[0],{});markers.setdefault(key,match[0]);found.setdefault(key,{})
 comments=inferred_comments(flat)
 result={key:{'key':key,'name':d.get('label',key),'representation':markers.get(key,'{$'+key+'}'),'notes':comments.get(key,d.get('description','')),'choices':[str(x) for x in d.get('enum',[])],'default':str(d.get('default','')),'color_mode':'auto','color':'#7DD3FC','inferred':True} for key,d in found.items()}
 for d in authored:result[d['key']]=d
 return list(result.values())

def enrich_record(record):
 definitions=registered(record)
 for d in definitions:
  if d.get('type')=='formatting':continue
  prefix='variables.'+d['key']+'.';flat=record['all_metadata']
  flat.setdefault(prefix+'type','string');flat[prefix+'label']=d['name'];flat[prefix+'description']=d['notes'];flat[prefix+'default']=d['default']
  if flat[prefix+'type']=='string':
   if d['choices']:flat[prefix+'enum']=d['choices']
   else:flat.pop(prefix+'enum',None)
 record['delimiters']=definitions_for_record(record)
 return record

def placeholders_in_text(text, fields, project_definitions=None):
 """Summarize recognized markers without changing the imported entry shape."""
 from model import delimiter_pattern, token_value

 record = {'text': text, 'all_metadata': fields, 'project_delimiters': project_definitions or []}
 definitions = definitions_for_record(record)
 authored = {item['representation']: item for item in definitions if not item.get('inferred')}
 names = {item['key']: item['name'] for item in definitions}
 pattern = re.compile(delimiter_pattern(authored.values()).pattern + r'|\$[bB]')
 occurrences = {}
 for match in pattern.finditer(text):
  marker = match[0]
  if marker in ('$B', '$b'):
   key, name, kind = 'line_break', 'Line break', 'formatting'
  else:
   definition = authored.get(marker)
   key = definition['key'] if definition else token_value(marker, {})[0]
   name = names.get(key, key)
   if name == key:name = key.replace('_', ' ').title()
   kind = definition.get('type', 'replacement') if definition else 'replacement'
  item = occurrences.setdefault((key, marker), {'marker': marker, 'key': key, 'name': name, 'type': kind, 'count': 0})
  item['count'] += 1
 return list(occurrences.values())

def describe(store,data):
 record=store.record(data['dataset'],int(data['index']))
 return {'revision':record['corpus_revision'],'source_revision':record['revision'],'definitions':record['delimiters']}

def export_file(store,data):
 record=store.record(data['dataset'],int(data['index']))
 return {'format':FILE_FORMAT,'version':1,'definitions':[dictionary_definition(item) for item in registered(record)]}

def dictionary_definition(definition):
 return {key:value for key,value in definition.items() if key not in {COMMENT_REVISION_ANCHOR,CHOICE_REVISION_ANCHOR}}

def import_file(store,data,*,profile=None):
 payload=data.get('file')
 if not isinstance(payload,dict) or payload.get('format')!=FILE_FORMAT or type(payload.get('version')) is not int or payload['version']!=1:
  raise ValueError('Choose a DMSpeaks delimiter dictionary (.dlmtr, .dlm or .json). Use Export to get an example.')
 items=payload.get('definitions')
 if not isinstance(items,list) or len(items)>LIMIT:
  raise ValueError(f'A delimiter file must contain a list of up to {LIMIT} definitions.')
 definitions=[]
 for index,item in enumerate(items,1):
  try:
   definitions.append(validate_definition(item))
  except ValueError as exc:
   raise ValueError(f'Definition {index}: {exc}') from exc
 for field in ('key','name','representation'):
  values=[d[field].casefold() if field=='name' else d[field] for d in definitions]
  if len(values)!=len(set(values)):
   raise ValueError(f'The file contains a duplicate delimiter {field}.')

 with store.lock:
  record=store.record(data['dataset'],int(data['index']))
  if record['revision']!=data.get('source_revision') or record['corpus_revision']!=data.get('expected_revision'):
   raise ValueError('The source or its definitions changed. Reopen Delimiters before importing.')
  from project_delimiter_library import read
  saved=read(store,record['dataset'])
  saved_by_key={d['key']:d for d in saved}
  current=record['delimiters']
  by_key={d['key']:d for d in current}
  by_marker={d['representation']:d for d in current}
  by_name={d['name'].casefold():d for d in current}
  added=[]
  for definition in definitions:
   definition=dictionary_definition(definition)
   key=definition['key']
   existing=saved_by_key.get(key)
   if existing and dictionary_definition(existing)==definition:
    continue
   check_authoring_marker(definition,profile,record)
   inferred=by_key.get(key)
   if existing or inferred and inferred['representation']!=definition['representation']:
    raise ValueError(f'{definition["name"]} conflicts with an existing delimiter key. Existing definitions are kept.')
   for match in (by_marker.get(definition['representation']),by_name.get(definition['name'].casefold())):
    if match and match['key']!=key:
     raise ValueError(f'{definition["name"]} conflicts with an existing delimiter name or marker.')
   added.append(definition)
  if len(saved)+len(added)>LIMIT:
   raise ValueError(f'A source can have up to {LIMIT} saved delimiters.')
  summary={'imported_keys':[d['key'] for d in added],'skipped':len(definitions)-len(added)}
  if not added:
   return summary
  return {**_write_project(store,record,saved+added,data),**summary}

def save(store,data,*,profile=None):
 with store.lock:
  record=store.record(data['dataset'],int(data['index']))
  if record['revision']!=data.get('source_revision'):raise ValueError('The source changed. Reopen Delimiters before saving.')
  from project_delimiter_library import read
  definition=validate_definition(data.get('definition'))
  local=local_registered(record)
  is_local=any(d['key']==definition['key'] for d in local)
  saved=local if is_local else read(store,record['dataset'])
  for other in record['delimiters']:
   if other['representation']!=definition['representation'] or other['key']==definition['key']:continue
   undeclared=not any(k.startswith('variables.'+other['key']+'.') for k in record['all_metadata'])
   if not (other.get('inferred') and not KEY.fullmatch(other['key']) and undeclared):
    raise ValueError('That delimiter string already belongs to another delimiter.')
  prior=next((d for d in record['delimiters'] if d['key']==definition['key']),None)
  if (not prior or prior.get('inferred') or prior['representation']!=definition['representation']
      or prior.get('type')!=definition.get('type')):
   check_authoring_marker(definition,profile,record)
  if data.get('create') is True and prior:raise ValueError('That variable key already exists. Select its delimiter to edit or insert it.')
  if data.get('create') is not None and (not prior or definition['name']!=prior['name']) and not NAME.fullmatch(definition['name']):
   raise ValueError('Use a delimiter name beginning with a letter or underscore, followed by letters, numbers or underscores.')
  if any(d['name'].casefold()==definition['name'].casefold() and d['key']!=definition['key'] for d in record['delimiters']):raise ValueError('Choose a unique delimiter name.')
  if prior and prior['representation']!=definition['representation'] and prior['representation'] in record['text']:raise ValueError('This delimiter string is in use. Keep it unchanged, or first replace its occurrences in the text.')
  if not is_local and prior and prior['representation']!=definition['representation']:
   from project_delimiter_library import used_in_entries
   uses=used_in_entries(store,record['dataset'],prior['representation'],saved)
   if uses:raise ValueError(f'This delimiter string is in use in entry {uses[0] + 1}. Keep it unchanged, or first replace its occurrences.')
  if any(d['key']==definition['key'] for d in saved):saved=[definition if d['key']==definition['key'] else d for d in saved]
  else:saved.append(definition)
  if len(saved)>LIMIT:raise ValueError(f'This source already defines {LIMIT} delimiters.')
  return (_write if is_local else _write_project)(store,record,saved,data,draft_text=data.get('draft_text'))

def delete(store,data):
 from model import delimiter_pattern,record_metadata
 with store.lock:
  record=store.record(data['dataset'],int(data['index']))
  if record['revision']!=data.get('source_revision'):raise ValueError('The source changed. Reopen Delimiters before deleting.')
  from project_delimiter_library import read
  local=local_registered(record)
  is_local=any(d['key']==data.get('key') for d in local)
  saved=local if is_local else read(store,record['dataset'])
  definition=next((d for d in saved if d['key']==data.get('key')),None)
  if definition is None:raise ValueError('Choose a saved delimiter to delete.')
  if any(m[0]==definition['representation'] for m in delimiter_pattern(saved).finditer(record['text'])):
   raise ValueError('Remove this delimiter’s occurrences from Source Text before deleting its definition.')
  data_view=store.load(record['dataset'])
  _,_,flat=record_metadata(data_view,record['index'])
  inherited={k:v for k,v in flat.items() if k not in {FIELD,FIELD+'_json'}}
  if any(d['key']==definition['key'] for d in definitions_for_record({'text':record['text'],'all_metadata':inherited})):
   raise ValueError('This delimiter is defined by the imported source. Its definition is still in use.')
  if not is_local:
   from project_delimiter_library import used_in_entries
   uses=used_in_entries(store,record['dataset'],definition['representation'],saved)
   if uses:raise ValueError(f'This delimiter is used in entry {uses[0] + 1}. Remove its occurrences before deleting it.')
  return (_write if is_local else _write_project)(store,record,[d for d in saved if d['key']!=definition['key']],data)

def _write_project(store,record,saved,data,*,draft_text=None):
 from project_delimiter_library import edit
 result=edit(store,record['dataset'],record['index'],saved,data.get('expected_revision'),draft_text=draft_text)
 updated=result['record']
 return {'revision':updated['corpus_revision'],'source_revision':updated['revision'],'definitions':updated['delimiters'],'record':updated,'history_id':result['history_id'],'history_scope':'project'}

def _write(store,record,saved,data,*,draft_text=None):
 from metadata_editor import edit
 from entry_delimiter_edit import plan_entry_delimiter_storage
 view=store.load(record['dataset']);row=view['rows'][record['index']]
 field,value,anchor_change=plan_entry_delimiter_storage(view['kind'],row,saved)
 change={'operation':'replace' if field in row else 'add','pointer':'/'+field,'value':value}
 updated=edit(store,record['dataset'],record['index'],'dialogue',change,data.get('expected_revision'),draft_text=draft_text,include_history_id=True,delimiter_anchor_change=anchor_change)
 return {'revision':updated['corpus_revision'],'source_revision':updated['revision'],'definitions':updated['delimiters'],'record':updated,'history_id':updated['metadata_history_id']}
