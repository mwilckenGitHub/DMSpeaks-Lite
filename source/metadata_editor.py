"""Transactional field edits on a working corpus; imported bytes never change.

This module deliberately does not guess relationships in custom JSON. Native
speaker identifiers are the one supported identifier rename operation.
"""
import copy,json,time
from functools import lru_cache
from json_fields import apply_field,escape,mapped_pointer,pointer_parts,pointer_get
from corpus_overlay import working_view
from corpus_structure_commands import StructureConflict
from model import getfield,setfield


def _row_prefix(data,index):
 return (('/'+escape(data['container'])) if data['container'] is not None else '')+'/'+str(index)


def _object(data,index,scope):
 root=data['root'];row=data['rows'][index]
 native=isinstance(root,dict) and root.get('format')=='open-speech-corpus'
 if scope=='dialogue':return row,_row_prefix(data,index)
 if scope=='corpus':
  if not isinstance(root,dict):raise ValueError('An array corpus has no root metadata object.')
  return root,''
 if scope=='speaker' and native:
  for i,speaker in enumerate(root.get('speakers',[])):
   if speaker.get('id')==row.get('speaker_ref'):return speaker,'/speakers/'+str(i)
  raise ValueError('This dialogue has no declared speaker.')
 if scope=='speaker':return row,_row_prefix(data,index)
 raise ValueError('Choose dialogue, speaker, or corpus metadata.')


def _dialogue_paths(data,row):
 mapping=data['mapping']
 source_paths=['/'+escape(key) for key in data['slots']]
 if mapping.get('text') and mapping['text']!='@segments':
  source_paths.append(mapped_pointer(row,mapping['text']))
 readonly=source_paths+[mapped_pointer(row,path) for key,path in mapping.items()
                         if path and (key=='id' or key.startswith('npc_'))]
 if _is_native(data):
  readonly+=['/id','/source/text','/speaker_ref','/voice_profile_ref','/entry_ref','/media_ref']
 return source_paths,readonly


def _is_native(data):
 return isinstance(data['root'],dict) and data['root'].get('format')=='open-speech-corpus'


def _protected(pointer,paths):
 return any(pointer==path or pointer.startswith(path+'/') or path.startswith(pointer+'/') for path in paths)


@lru_cache(maxsize=1)
def _entry_enums():
 from osc_validator import _load_schema
 definitions=_load_schema()['$defs']
 return (definitions['source']['properties']['format']['enum'],
         definitions['variable_definition']['properties']['type']['enum'])


def _entry_choices(data,row):
 if not _is_native(data):return {}
 formats,types=_entry_enums()
 choices={'/source/format':formats}
 for name,variable in row.get('variables',{}).items():
  path='/variables/'+escape(name)
  choices[path+'/type']=types
  if variable.get('enum'):
   choices[path+'/default']=variable['enum']
 return choices


def _inline_paths(data,row):
 from corpus_lookup import EXTENSION,FIELD,ACTIVE
 from delimiter_definitions import INFERRED_COMMENTS_FIELD
 from entry_delimiter_edit import ENTRY_AUDIO_ANCHOR
 source_paths,readonly=_dialogue_paths(data,row)
 readonly += ['/_voxweave_audio_sources','/_voxweave_audio_sources_json','/_dmspeaks_clip_groups',
              '/_dmspeaks_clip_groups_json','/_dmspeaks_delimiters','/_dmspeaks_delimiters_json','/source_history']
 readonly += ['/'+key for key in (EXTENSION,FIELD,ACTIVE,ENTRY_AUDIO_ANCHOR,INFERRED_COMMENTS_FIELD)]
 # This import supplies a normalized transcript alongside the original dialogue.
 if (data['mapping']['text']=='originalText' and isinstance(data['root'],dict)
     and {'schemaVersion','lineCount','npcs','spawns'}<=data['root'].keys()):
  source_paths.append('/text')
  readonly+=['/text','/questId','/fileName']
 return source_paths,readonly


def describe(store,dataset,index,scope='dialogue'):
 with store.lock:
  return _describe_data(store.load(dataset),index,scope)


def _describe_data(data,index,scope):
 if not 0<=index<len(data['rows']):raise ValueError('Unknown record.')
 obj,prefix=_object(data,index,scope)
 readonly=[];entry={}
 if scope=='dialogue':
  from corpus_lookup import EXTENSION,FIELD,field_types
  from delimiter_definitions import INFERRED_COMMENTS_FIELD
  from entry_delimiter_edit import ENTRY_AUDIO_ANCHOR
  source_paths,readonly=_inline_paths(data,obj)
  obj={key:value for key,value in obj.items() if key not in {EXTENSION,FIELD,ENTRY_AUDIO_ANCHOR,INFERRED_COMMENTS_FIELD}}
  entry={'source_paths':source_paths,'choices':_entry_choices(data,obj),
         'type_policy':'csv' if data['kind']=='csv' else 'native' if _is_native(data) else 'custom',
         'field_types':field_types(data)}
 if scope=='speaker' and not (isinstance(data['root'],dict) and data['root'].get('format')=='open-speech-corpus'):
  speaker_fields={key:value for key,value in obj.items() if key.startswith('npc_')}
  for key,path in data['mapping'].items():
   if key.startswith('npc_') and path and getfield(obj,path) is not None:setfield(speaker_fields,path,getfield(obj,path))
  obj=speaker_fields
  if data['mapping'].get('npc_id'):readonly=[mapped_pointer(obj,data['mapping']['npc_id'])]
 if scope=='corpus':obj={key:value for key,value in obj.items() if key not in {data['container'],'speakers','voice_profiles','takes','clips','media','audio_sources','render_bindings'}}
 return {'revision':data.get('overlay_revision',0),'scope':scope,'fields':copy.deepcopy(obj),'pointer':prefix,'readonly_paths':readonly,**entry,'notice':'Changes affect the working corpus. Imported bytes remain unchanged. Unknown custom relationships are not inferred.'}


def describe_structure(store,dataset,index):
 """Describe a stable entry using the same field rules as the current editor."""
 from corpus_structure_commands import read_state
 with store.lock:
  own_transaction=not store.db.in_transaction
  if own_transaction:store.db.execute('BEGIN')
  try:
   structure=read_state(store,dataset)
   data,_,_=store._structure_views(dataset,index,structure['snapshot'])
   result=_describe_data(data,index,'dialogue')
   result['structure_revision']=structure['revision']
   if index<structure['snapshot']['original_count']:
    row=data['rows'][index]
    result['definition_readonly_paths']=[mapped_pointer(row,path) for path in data['mapping'].values() if path and path!='@segments']
   if own_transaction:store.db.commit()
   return result
  except BaseException:
   if own_transaction:store.db.rollback()
   raise


def save_added_structure_entry(store,dataset,index,text,values,*,expected_structure_revision,
                               expected_source_revision,expected_metadata_revision):
 """Save a new entry's form and text as one undoable structural edit.

 This internal service is deliberately not exposed until the workspace can
 consistently open, save, recover and export structural edits.
 """
 from corpus_structure_commands import StructureConflict,read_state,save_added_entry
 if not isinstance(text,str) or len(text)>50000:
  raise ValueError('A source string must be text of at most 50,000 characters.')
 with store.lock:
  if store.db.in_transaction:
   raise ValueError('Cannot save an entry inside another transaction.')
  store.db.execute('BEGIN')
  try:
   structure=read_state(store,dataset)
   if type(expected_structure_revision) is not int or structure['revision']!=expected_structure_revision:
    raise StructureConflict('The entry list changed. Refresh before saving.')
   snapshot=structure['snapshot']
   if type(index) is not int or index not in snapshot['state']['order'] or str(index) not in snapshot['added_entries']:
    raise ValueError('Choose a visible added entry.')
   record=store.record_structure(dataset,index)
   if record['revision']!=expected_source_revision or record['metadata_revision']!=expected_metadata_revision:
    raise StructureConflict('This entry changed. Refresh before saving.')
   from project_delimiter_library import validate_new_source_markers
   validate_new_source_markers(store,dataset,record['text'],text)
   data,_,_=store._structure_views(dataset,index,snapshot)
   row=copy.deepcopy(data['rows'][index])
   commands,_=apply_entry_values(data,row,values)
   metadata=copy.deepcopy(snapshot['added_entries'][str(index)]['metadata'])
   for command in commands:apply_field(metadata,command)
   store.db.commit()
  except BaseException:
   store.db.rollback()
   raise
  # Recheck every revision in the write transaction: another process can have
  # edited this entry between the read snapshot and the write lock.
  return save_added_entry(store,dataset,index,text,metadata,
                          expected_structure_revision=expected_structure_revision,
                          expected_source_revision=expected_source_revision,
                          expected_metadata_revision=expected_metadata_revision)


def _list_edit(row, operation):
 """Turn one list edit into a reversible replacement of that metadata field."""
 if operation.get('operation') not in {'append_item','remove_item'}:return operation
 pointer=operation.get('pointer','');items=pointer_get(row,pointer)
 if not isinstance(items,list):raise ValueError('Choose a list field.')
 items=copy.deepcopy(items)
 if operation['operation']=='append_item':
  if len(items)>=10000:raise ValueError('This list has reached its item limit.')
  if 'value' not in operation:raise ValueError('Choose a value for the new list item.')
  items.append(copy.deepcopy(operation['value']))
 else:
  position=operation.get('index')
  if type(position) is not int or not 0<=position<len(items):raise ValueError('Choose an existing list item.')
  items.pop(position)
 return {'operation':'replace','pointer':pointer,'value':items}


def edit_added_structure_field(store,dataset,index,operation,*,expected_structure_revision,
                               expected_source_revision,expected_metadata_revision):
 """Add or remove a metadata field on a newly allocated entry atomically."""
 from corpus_structure_commands import StructureConflict,read_state,save_added_entry
 if not isinstance(operation,dict) or operation.get('operation') not in {'add','remove','append_item','remove_item'}:
  raise ValueError('Choose Add Field or Remove Field.')
 with store.lock:
  if store.db.in_transaction:raise ValueError('Cannot edit a field inside another transaction.')
  store.db.execute('BEGIN')
  try:
   structure=read_state(store,dataset)
   if type(expected_structure_revision) is not int or structure['revision']!=expected_structure_revision:
    raise StructureConflict('The entry list changed. Refresh before saving.')
   snapshot=structure['snapshot']
   if type(index) is not int or index not in snapshot['state']['order'] or str(index) not in snapshot['added_entries']:
    raise ValueError('Choose a visible added entry.')
   record=store.record_structure(dataset,index)
   if record['revision']!=expected_source_revision or record['metadata_revision']!=expected_metadata_revision:
    raise StructureConflict('This entry changed. Refresh before saving.')
   data,_,_=store._structure_views(dataset,index,snapshot)
   pointer=operation.get('pointer','')
   _,readonly=_inline_paths(data,data['rows'][index])
   parts=pointer_parts(pointer)
   if not parts or not parts[-1].strip():raise ValueError('Enter a field name.')
   if _protected(pointer,readonly):raise ValueError('This entry field is read only.')
   if _is_native(data) and not pointer.startswith('/metadata/'):
    raise ValueError('Add custom fields within the native metadata object.')
   operation=_list_edit(data['rows'][index],operation)
   _,mapping,commands,_=_prepare(data,index,'dialogue',operation)
   if mapping!=data['mapping'] or len(commands)!=1:
    raise ValueError('Mapped corpus fields cannot be changed for only one entry.')
   metadata=copy.deepcopy(snapshot['added_entries'][str(index)]['metadata'])
   apply_field(metadata,operation)
   store.db.commit()
  except BaseException:
   store.db.rollback()
   raise
  return save_added_entry(store,dataset,index,record['text'],metadata,
                          expected_structure_revision=expected_structure_revision,
                          expected_source_revision=expected_source_revision,
                          expected_metadata_revision=expected_metadata_revision)


def edit_original_structure_field(store,dataset,index,operation,*,expected_structure_revision,
                                  expected_source_revision,expected_metadata_revision):
 """Edit an original entry's fields while checking its structural visibility."""
 from corpus_structure_commands import StructureConflict,read_state
 if not isinstance(operation,dict) or operation.get('operation') not in {'add','remove','append_item','remove_item'}:
  raise ValueError('Choose Add Field or Remove Field.')
 with store.lock:
  structure=read_state(store,dataset)
  if type(expected_structure_revision) is not int or structure['revision']!=expected_structure_revision:
   raise StructureConflict('The entry list changed. Refresh before saving.')
  snapshot=structure['snapshot']
  if type(index) is not int or index not in snapshot['state']['order'] or index>=snapshot['original_count']:
   raise ValueError('Choose a visible original entry.')
  record=store.record_structure(dataset,index)
  if record['revision']!=expected_source_revision or record['metadata_revision']!=expected_metadata_revision:
   raise StructureConflict('This entry changed. Refresh before saving.')
  data=store.load(dataset);row=data['rows'][index]
  pointer=operation.get('pointer','');parts=pointer_parts(pointer)
  if not parts or not parts[-1].strip():raise ValueError('Enter a field name.')
  _,readonly=_inline_paths(data,row)
  readonly += [mapped_pointer(row,path) for path in data['mapping'].values() if path and path!='@segments']
  if _protected(pointer,readonly):raise ValueError('This entry field is read only.')
  operation=_list_edit(row,operation)
  result=edit(store,dataset,index,'dialogue',operation,record['corpus_revision'],
              draft_text=record['text'],include_history_id=True,
              expected_structure={'revision':expected_structure_revision,'index':index})
  return {'record':result,'history_id':result['metadata_history_id']}


def _prepare(data,index,scope,operation,*,inferred_comment=False):
 from corpus_lookup import EXTENSION,FIELD,ACTIVE
 from delimiter_definitions import INFERRED_COMMENTS_FIELD
 from entry_delimiter_edit import ENTRY_AUDIO_ANCHOR
 root=copy.deepcopy(data['root']);mapping=copy.deepcopy(data['mapping']);view={**data,'root':root,'rows':root if data['container'] is None else root[data['container']]}
 obj,prefix=_object(view,index,scope);relative=operation.get('pointer','');parts=pointer_parts(relative)
 if not parts:raise ValueError('Select a metadata field.')
 if ENTRY_AUDIO_ANCHOR in (parts[0],operation.get('new_key')):raise ValueError('This field is managed by DMSpeaks.')
 if not inferred_comment and INFERRED_COMMENTS_FIELD in (parts[0],operation.get('new_key')):
  raise ValueError('Use Delimiters to edit these comments.')
 if parts[0] in {EXTENSION,FIELD,ACTIVE} or operation.get('new_key') in (EXTENSION,FIELD,ACTIVE):raise ValueError('Use the Corpus Lookup Table to edit lookup rules.')
 if parts[0] in {'_voxweave_audio_sources','_voxweave_audio_sources_json'}:raise ValueError('Use Audio sources to edit the reference registry.')
 if parts[0] in {'_dmspeaks_clip_groups','_dmspeaks_clip_groups_json'}:raise ValueError('Use replacement Group settings to edit this authoring registry.')
 # A corpus editor is not a row/resource deletion tool.
 if scope=='corpus' and parts[0] in {data['container'],'speakers','voice_profiles','takes','clips','media','audio_sources','render_bindings','format','version','corpus_id'}:raise ValueError('Use the dedicated editor for corpus records, resources, and identity.')
 row=view['rows'][index];native=isinstance(root,dict) and root.get('format')=='open-speech-corpus'
 if scope=='dialogue':
  _,protected=_dialogue_paths(data,row)
  if _protected(relative,protected):raise ValueError('Edit source text in the text editor, and speaker-owned fields in Speaker metadata. Stable dialogue IDs are protected.')
 if scope=='speaker':
  if native:
   if parts[0]=='id' and (relative!='/id' or operation['operation']!='replace'):raise ValueError('Change the speaker ID value; do not remove or rename its field.')
  else:
   owned={path for key,path in mapping.items() if key.startswith('npc_') and path}
   if relative not in {mapped_pointer(row,path) for path in owned} and not parts[0].startswith('npc_'):raise ValueError('Legacy speaker editing supports mapped NPC fields. Add custom fields to the dialogue instead.')
   if relative==mapped_pointer(row,mapping.get('npc_id','')):raise ValueError('Legacy NPC identifiers are protected; custom relationships cannot be inferred.')
 operations=[];mapped_key=next((key for key,path in mapping.items() if path and mapped_pointer(row,path)==relative),None)
 # A mapped header rename applies across rows so the mapping remains consistent.
 if operation['operation']=='rename' and mapped_key:
  old_path=mapping[mapped_key];new_key=operation.get('new_key');original_parts=pointer_parts(relative)
  new_parts=[*original_parts[:-1],new_key]
  if any('.' in part for part in new_parts) and old_path not in row:raise ValueError('Nested mapped field names cannot contain dots.')
  new_path=new_key if old_path in row else '.'.join(new_parts)
  for i,item in enumerate(view['rows']):
   try:pointer_get(item,relative)
   except ValueError:continue
   operations.append({**operation,'pointer':_row_prefix(view,i)+relative})
  mapping[mapped_key]=new_path
 elif scope=='speaker' and not native:
  actor=getfield(row,mapping.get('npc_id'))
  if actor is None:raise ValueError('A shared speaker edit requires a mapped NPC ID.')
  for i,item in enumerate(view['rows']):
   if getfield(item,mapping.get('npc_id'))==actor:
    item_operation=dict(operation)
    try:pointer_get(item,relative)
    except ValueError:
     if operation['operation']=='remove':continue
     if operation['operation']=='replace':item_operation['operation']='add'
    operations.append({**item_operation,'pointer':_row_prefix(view,i)+relative})
 else:operations=[{**operation,'pointer':prefix+relative}]
 # Rename the native identity plus only declared speaker references.
 if scope=='speaker' and native and relative=='/id':
  old=obj['id'];new=operation.get('value')
  if not isinstance(new,str) or not new.strip() or any(s.get('id')==new for s in root.get('speakers',[])):raise ValueError('Choose a new, unique speaker ID.')
  for collection in ['entries','speakers','voice_profiles','takes','clips']:
   for i,item in enumerate(root.get(collection,[])):
    if item.get('speaker_ref')==old:operations.append({'operation':'replace','pointer':'/'+collection+'/'+str(i)+'/speaker_ref','value':new})
  for provider,bindings in root.get('render_bindings',{}).items():
   old_key=str(root.get('audio_sources_dataset_id',''))+':'+old
   # Imported registries may retain their original dataset prefix.
   for key in list(bindings):
    if key==old_key:operations.append({'operation':'rename','pointer':'/render_bindings/'+escape(provider)+'/'+escape(key),'new_key':key[:-len(old)]+new})
 inverses=[]
 for item in operations:inverses.insert(0,apply_field(root,item))
 # Optional mapped fields may disappear; a globally absent mapping is cleared.
 if operation['operation']=='remove' and mapped_key:
  if not any(getfield(item,mapping[mapped_key]) is not None for item in view['rows']):mapping[mapped_key]=''
 if data['kind']=='csv' and any(isinstance(value,(dict,list)) for item in view['rows'] for value in item.values()):raise ValueError('CSV metadata fields must be scalar values; use JSON for structured fields.')
 return root,mapping,operations,inverses


def edit(store,dataset,index,scope,operation,expected_revision,*,draft_text=None,include_history_id=False,expected_structure=None,delimiter_anchor_change=None,inferred_comment=False):
 with store.lock:
  data=store.load(dataset)
  if type(expected_revision) is not int or expected_revision!=data.get('overlay_revision',0):raise ValueError('The corpus changed. Refresh before applying this edit.')
  if not 0<=index<len(data['rows']):raise ValueError('Unknown record.')
  root,mapping,operations,inverses=_prepare(data,index,scope,operation,inferred_comment=inferred_comment)
  if delimiter_anchor_change is not None:
   change={**delimiter_anchor_change,'pointer':_row_prefix(data,index)+delimiter_anchor_change['pointer']}
   inverses.insert(0,apply_field(root,change))
   operations.append(change)
  candidate=working_view(store.original_load(dataset),root,mapping,expected_revision+1)
  command={'operations':operations,'mapping':mapping};inverse={'operations':inverses,'mapping':data['mapping']}
  if draft_text is not None:
   if not isinstance(draft_text,str) or len(draft_text)>50000:raise ValueError('A source string must be text of at most 50,000 characters.')
   record=store.record(dataset,index)
   command['draft']={'index':index,'before':record['text'],'after':draft_text}
   inverse['draft']={'index':index,'before':draft_text,'after':record['text']}
  history_id=_commit(store,dataset,expected_revision,candidate,command,inverse,draft=command.get('draft'),expected_structure=expected_structure)
  result=store.record(dataset,index)
  if include_history_id:result['metadata_history_id']=history_id
  return result


def apply_entry_values(data,row,values,prefix=''):
 """Apply typed replacements to a copy of one entry and return history operations."""
 if not isinstance(values,dict):
  raise ValueError('Entry field edits must be an object.')
 _,readonly=_inline_paths(data,row)
 commands=[];inverses=[]
 for pointer,value in values.items():
  if not pointer_parts(pointer) or _protected(pointer,readonly):
   raise ValueError('This entry field is read only.')
  current=pointer_get(row,pointer)
  scalar_types=(str,bool,int,float,type(None))
  if type(current) not in scalar_types or type(value) not in scalar_types:
   raise ValueError('Use Properties to change an object or list.')
  same_kind=(type(current) is type(value) or type(current) in (int,float) and type(value) in (int,float))
  if not same_kind and (data['kind']=='csv' or _is_native(data) and not pointer.startswith('/metadata/')):
   raise ValueError('This field must keep its existing type.')
  if same_kind and current==value:continue
  command={'operation':'replace','pointer':pointer,'value':value}
  inverse=apply_field(row,command)
  commands.append({**command,'pointer':prefix+pointer})
  inverses.insert(0,{**inverse,'pointer':prefix+pointer})
 return commands,inverses


def save_entry(store,dataset,index,text,values,expected_revision,expected_source_revision,*,expected_structure=None):
 """Save the form and dialogue text in a single transaction and history entry."""
 if expected_structure is not None and (not isinstance(expected_structure,dict) or expected_structure.get('index')!=index):raise ValueError('Invalid entry precondition.')
 if not isinstance(text,str) or len(text)>50000:
  raise ValueError('A source string must be text of at most 50,000 characters.')
 with store.lock:
  record=store.record(dataset,index)
  if record['revision']!=expected_source_revision:
   raise StructureConflict('This dialogue was changed elsewhere. Copy your unsaved edits before reopening the dialogue.')
  if type(expected_revision) is not int or expected_revision!=record['corpus_revision']:
   raise StructureConflict('The corpus changed elsewhere. Copy your unsaved edits before reopening the dialogue.')
  data=store.load(dataset)
  if not isinstance(values,dict):raise ValueError('Entry field edits must be an object.')
  if values:
   root=copy.deepcopy(data['root'])
   row=(root if data['container'] is None else root[data['container']])[index]
   operations,inverses=apply_entry_values(data,row,values,_row_prefix(data,index))
  else:
   operations=[]
  history_id=None
  if operations:
   if expected_structure is not None:
    from project_delimiter_library import validate_new_source_markers
    validate_new_source_markers(store,dataset,record['text'],text)
   mapping=data['mapping']
   candidate=working_view(store.original_load(dataset),root,mapping,expected_revision+1)
   command={'operations':operations,'mapping':mapping}
   inverse={'operations':inverses,'mapping':mapping}
   if text!=record['text']:
    command['draft']={'index':index,'before':record['text'],'after':text}
    inverse['draft']={'index':index,'before':text,'after':record['text']}
   history_id=_commit(store,dataset,expected_revision,candidate,command,inverse,draft=command.get('draft'),expected_structure=expected_structure)
  elif text!=record['text']:
   store.save_draft(dataset,index,text,expected_revision=expected_source_revision,expected_structure=expected_structure,
                    require_dictionary=expected_structure is not None)
  return {'record':store.record(dataset,index),'entry_fields':describe(store,dataset,index),'history_id':history_id}


def _commit(store,dataset,revision,candidate,command=None,inverse=None,history=None,draft=None,project_delimiters=None,expected_structure=None,allow_added_structure=False,expected_entry=None,safe_marker_uses=None,allow_lookup_structure=False):
 from source_text_lock import check_overlay
 check_overlay(store,dataset,candidate,draft,structured=allow_added_structure)
 try:
  store.db.execute('begin immediate')
  row=store.db.execute('select revision from corpus_overlays where dataset=?',(dataset,)).fetchone()
  if (row[0] if row else 0)!=revision:raise ValueError('The corpus changed. Refresh before applying this edit.')
  if expected_structure is not None:
   from corpus_structure_commands import require_visible
   if allow_added_structure and project_delimiters is None:raise ValueError('Only project delimiter changes may target an added entry.')
   if draft is not None and draft['index']!=expected_structure['index']:raise ValueError('The dialogue does not match the selected entry.')
   require_visible(store,dataset,expected_structure,original_only=not (allow_added_structure or allow_lookup_structure))
   if expected_entry is not None:
    record=store.record_structure(dataset,expected_structure['index'])
    if (record['revision'],record['metadata_revision'])!=expected_entry:raise ValueError('This entry changed elsewhere. Refresh before editing delimiters.')
  if draft is not None:
   index=draft['index'];source=store.record_structure(dataset,index) if allow_added_structure else store.record(dataset,index)
   if source['text']!=draft['before']:raise ValueError('The source changed. Restore the expected text before replaying this edit.')
   if draft['after']==source['original_text']:
    store.db.execute('delete from drafts where dataset=? and record=?',(dataset,index))
   else:
    store.db.execute('insert or replace into drafts values(?,?,?,?)',(dataset,index,draft['after'],time.time()))
  if project_delimiters is not None:
   from project_delimiter_library import read, setting_key, validate
   if read(store,dataset)!=project_delimiters['before']:
    raise ValueError('The project delimiters changed. Refresh before applying this edit.')
   definitions=validate(project_delimiters['after'])
   # Recheck inside the write transaction: another window may have added a
   # retained entry after the interface prepared the delimiter change.
   from project_delimiter_library import source_conflicts, used_in_entries, marker_use_fingerprints
   conflicts=source_conflicts(store,dataset,definitions)
   if conflicts:
    entry,key,kind,value=conflicts[0]
    raise ValueError(f'Delimiter {key} conflicts with {kind} {value} in entry {entry + 1}.')
   replacement_by_key={definition['key']:definition for definition in definitions}
   for prior in project_delimiters['before']:
    replacement=replacement_by_key.get(prior['key'])
    if replacement and replacement['representation']==prior['representation']:continue
    uses=used_in_entries(store,dataset,prior['representation'],project_delimiters['before'])
    # Undo can restore unchanged text that contained this marker before it
    # became a delimiter. A new or changed use must keep its definition.
    if history and uses:
     current=marker_use_fingerprints(store,dataset,[prior['representation']],project_delimiters['before'])[prior['representation']]
     safe=(safe_marker_uses or {}).get(prior['representation'],{})
     uses=[index for index in uses if current.get(str(index))!=safe.get(str(index))]
    if uses:raise ValueError(f'This delimiter is used in entry {uses[0] + 1}. Remove its occurrences before changing it.')
   store.db.execute('insert or replace into settings values(?,?)',(setting_key(dataset),json.dumps(definitions,ensure_ascii=False)))
  previous=store.load(dataset)['root'];following=candidate['root']
  if isinstance(previous,dict) and isinstance(following,dict) and previous.get('format')=='open-speech-corpus':
   renames=[(a['id'],b['id']) for a,b in zip(previous.get('speakers',[]),following.get('speakers',[])) if a['id']!=b['id']]
   for setting_key in ['workspace','corpus_audio_sources:'+dataset]:
    saved=store.db.execute('select value from settings where key=?',(setting_key,)).fetchone()
    if not saved:continue
    value=json.loads(saved[0])
    for choices in value.get('voice_bindings',{}).values():
     for old,new in renames:
      before=dataset+':'+old;after=dataset+':'+new
      if before in choices:
       if after in choices:raise ValueError('The new speaker ID already has a saved voice binding.')
       choices[after]=choices.pop(before)
    store.db.execute('update settings set value=? where key=?',(json.dumps(value),setting_key))
  store.db.execute('insert or replace into corpus_overlays values(?,?,?,?)',(dataset,json.dumps(candidate['root'],ensure_ascii=False,allow_nan=False),json.dumps(candidate['mapping']),revision+1))
  if history:
   history_id,undone=history
   store.db.execute('update corpus_metadata_history set undone=? where id=?',(undone,history_id))
  else:
   store.db.execute('delete from corpus_metadata_history where dataset=? and undone=1',(dataset,))
   history_id=store.db.execute('insert into corpus_metadata_history(dataset,command,inverse,created) values(?,?,?,?)',(dataset,json.dumps(command),json.dumps(inverse),time.time())).lastrowid
  store.db.commit()
 except Exception:
  store.db.rollback();raise
 store.cache[dataset]=candidate
 return history_id


def history(store,dataset,index,expected_revision,redo=False,*,expected_history_id=None,expected_structure=None,allow_added_structure=False,allow_lookup_structure=False):
 if expected_structure is not None and (not isinstance(expected_structure,dict) or expected_structure.get('index')!=index):raise ValueError('Invalid entry precondition.')
 with store.lock:
  data=store.load(dataset)
  if type(expected_revision) is not int or expected_revision!=data.get('overlay_revision',0):raise ValueError('The corpus changed. Refresh before undoing metadata.')
  if not 0<=index<len(data['rows']) and not ((allow_added_structure or allow_lookup_structure) and expected_structure is not None):raise ValueError('Unknown record.')
  row=store.db.execute('select id,command,inverse from corpus_metadata_history where dataset=? and undone=? order by id '+('asc' if redo else 'desc')+' limit 1',(dataset,int(redo))).fetchone()
  if not row:raise ValueError('No metadata change to '+('redo.' if redo else 'undo.'))
  if expected_history_id is not None and row[0]!=expected_history_id:raise ValueError('The metadata history has changed. Review the latest corpus edits first.')
  command=json.loads(row[1 if redo else 2]);root=copy.deepcopy(data['root'])
  if allow_added_structure and (not command.get('project_delimiters') or command.get('operations')):
   raise ValueError('This history item is not a project delimiter change.')
  if allow_lookup_structure:
   from corpus_lookup import location
   operations=command.get('operations',[])
   if command.get('draft') or command.get('project_delimiters') or len(operations)!=1 or operations[0].get('pointer')!=location(data)[2]:
    raise ValueError('This history item is not a lookup change.')
  for operation in command['operations']:apply_field(root,operation)
  candidate=working_view(store.original_load(dataset),root,command['mapping'],expected_revision+1)
  _commit(store,dataset,expected_revision,candidate,history=(row[0],0 if redo else 1),draft=command.get('draft'),project_delimiters=command.get('project_delimiters'),expected_structure=expected_structure,allow_added_structure=allow_added_structure,safe_marker_uses=command.get('safe_marker_uses'),allow_lookup_structure=allow_lookup_structure)
  return store.record_structure(dataset,index) if (allow_added_structure or allow_lookup_structure) else store.record(dataset,index)
