"""Per-occurrence authoring state, independent of TTS identity and source bytes.

Local Qwen registry outline corrected for SQLite JSON, revision checks,
occurrence ownership, inactive history and transactional portable restore.
"""
import copy,hashlib,json
from clip_group_schema import validate_group,effective_effects,effective_section_effects
from performance_plan import parse_record

FORMAT='dmspeaks-clip-groups'
IDENTITY=('id','slot_order','source_revision','source_start','source_end','placeholder')

def _key(dataset,index):return f'clip_groups:{dataset}:{index}'

def _catalog(raw,record_id=None):
 if not isinstance(raw,dict) or set(raw)!={'revision','dialogue_id','groups'}:raise ValueError('Invalid replacement group catalogue.')
 if type(raw['revision']) is not int or raw['revision']<0:raise ValueError('Invalid replacement group revision.')
 if not isinstance(raw['dialogue_id'],(str,int)) or isinstance(raw['dialogue_id'],bool) or record_id is not None and raw['dialogue_id']!=record_id:raise ValueError('Replacement groups belong to another dialogue.')
 if not isinstance(raw['groups'],list):raise ValueError('Replacement groups must be an array.')
 groups=[validate_group(g) for g in raw['groups']]
 if len({g['id'] for g in groups})!=len(groups):raise ValueError('Duplicate replacement group ID.')
 return {'revision':raw['revision'],'dialogue_id':raw['dialogue_id'],'groups':groups}

def load(store,record):
 saved=store.setting(_key(record['dataset'],record['index']))
 if saved is None and not store.setting('clip_groups_snapshot:'+record['dataset'],False):
  saved=next((e for e in store.load(record['dataset']).get('clip_group_config',{}).get('dialogues',[]) if e['index']==record['index']),None)
  if saved is not None:saved={k:v for k,v in saved.items() if k!='index'}
 return _catalog(saved if saved is not None else {'revision':0,'dialogue_id':record['id'],'groups':[]},record['id'])

def default_group(record,segment):
 material=[record['id'],record['revision'],segment['order'],segment['source_start'],segment['source_end']]
 ident='group-'+hashlib.sha256(json.dumps(material,ensure_ascii=False).encode()).hexdigest()[:20]
 from delimiter_definitions import registered
 definition=next((d for d in registered(record) if d['representation']==segment['placeholder']),None)
 color={'color_mode':definition['color_mode'],'color':definition['color'] if definition['color_mode']=='manual' else None} if definition else {}
 return validate_group({'id':ident,'slot_order':segment['order'],'source_revision':record['revision'],
  'source_start':segment['source_start'],'source_end':segment['source_end'],'placeholder':segment['placeholder'],**color})

def describe(store,record,segments,catalog=None):
 catalog=load(store,record) if catalog is None else _catalog(catalog,record['id'])
 stored={g['id']:g for g in catalog['groups']};active=[];used=set()
 for segment in segments:
  if segment['type']=='fixed' or segment.get('speech_excluded'):continue
  default=default_group(record,segment);saved=stored.get(default['id'])
  if saved is not None:
   if any(saved[k]!=default[k] for k in IDENTITY):raise ValueError('Replacement group bounds disagree with this source.')
   used.add(saved['id'])
  active.append(copy.deepcopy(saved or default))
 return {'revision':catalog['revision'],'groups':active,'inactive_count':len(stored)-len(used)}

def save(app,data):
 with app.store.lock:
  record=app.record(data);catalog=load(app.store,record)
  if data.get('source_revision')!=record['revision']:raise ValueError('The source changed. Reopen this replacement group.')
  if type(data.get('expected_revision')) is not int or data['expected_revision']!=catalog['revision']:raise ValueError('Replacement group settings changed. Refresh before saving.')
  group=validate_group(data.get('group'));segments=parse_record(record,data.get('player',{}),data)['segments']
  expected=next((g for g in describe(app.store,record,segments,catalog)['groups'] if g['id']==group['id']),None)
  if expected is None or any(group[k]!=expected[k] for k in IDENTITY):raise ValueError('Choose a delimiter occurrence in this source revision.')
  for ident in group['candidate_overrides']:
   asset=app.store.asset(ident)
   if asset.get('kind')!='piece' or asset.get('dataset') not in (None,'',record['dataset']) or asset.get('line_id')!=record['id'] or asset.get('slot_order')!=group['slot_order'] or asset.get('source_revision')!=record['revision']:
    raise ValueError('A candidate override belongs to another occurrence or source revision.')
  updated=[g for g in catalog['groups'] if g['id']!=group['id']]+[group]
  catalog={'revision':catalog['revision']+1,'dialogue_id':record['id'],'groups':updated}
  app.store.set_setting(_key(record['dataset'],record['index']),catalog)
  return describe(app.store,record,segments,catalog)

def export_config(store,dataset):
 parsed=store.load(dataset);count=len(parsed['rows']);prefix=f'clip_groups:{dataset}:'
 from corpus_structure_snapshot import read as read_structure
 structure=read_structure(store,dataset)
 if structure is not None:count=structure['state']['next_slot']
 with store.lock:rows=store.db.execute('select key,value from settings where substr(key,1,?)=?',(len(prefix),prefix)).fetchall()
 entries={} if store.setting('clip_groups_snapshot:'+dataset,False) else {e['index']:copy.deepcopy(e) for e in parsed.get('clip_group_config',{}).get('dialogues',[])}
 for key,raw in rows:
  index=key[len(prefix):]
  if not index.isdecimal():raise ValueError('Invalid saved replacement group index.')
  entries[int(index)]={'index':int(index),**_catalog(json.loads(raw))}
 return validate_config({'format':FORMAT,'version':'0.1','dialogues':sorted(entries.values(),key=lambda e:e['index'])},count)

def validate_config(config,row_count,asset_ids=None):
 if not isinstance(config,dict) or set(config)!={'format','version','dialogues'} or config['format']!=FORMAT or config['version']!='0.1' or not isinstance(config['dialogues'],list):raise ValueError('Invalid replacement group configuration.')
 if type(row_count) is not int or row_count<0:raise ValueError('Invalid dialogue count.')
 entries=[];seen=set()
 for entry in config['dialogues']:
  if not isinstance(entry,dict) or set(entry)!={'index','dialogue_id','revision','groups'}:raise ValueError('Invalid replacement group dialogue.')
  index=entry['index']
  if type(index) is not int or not 0<=index<row_count or index in seen:raise ValueError('Invalid or duplicate replacement group dialogue index.')
  seen.add(index);catalog=_catalog({k:v for k,v in entry.items() if k!='index'})
  if asset_ids is not None:
   for group in catalog['groups']:
    if set(group['candidate_overrides'])-asset_ids:raise ValueError('A replacement group references missing candidate audio.')
  entries.append({'index':index,**catalog})
 return {'format':FORMAT,'version':'0.1','dialogues':entries}

def restore_config(store,dataset,config,id_map):
 from corpus_structure_snapshot import read as read_structure
 structure=read_structure(store,dataset)
 count=structure['state']['next_slot'] if structure is not None else len(store.load(dataset)['rows'])
 validated=validate_config(config,count,set(id_map))
 prepared=[]
 for entry in validated['dialogues']:
  record=store.record_structure(dataset,entry['index'],include_hidden=True) if structure is not None else store.record(dataset,entry['index'])
  catalog=_catalog({k:v for k,v in entry.items() if k!='index'},record['id'])
  for group in catalog['groups']:
   group['candidate_overrides']={id_map[k]:v for k,v in group['candidate_overrides'].items()}
  prepared.append((_key(dataset,entry['index']),catalog))
 with store.lock:
  # Validation is complete before any write. Keep all groups in one transaction.
  with store.db:
   prefix=f'clip_groups:{dataset}:'
   store.db.execute('delete from settings where substr(key,1,?)=?',(len(prefix),prefix))
   store.db.execute('insert or replace into settings values(?,?)',('clip_groups_snapshot:'+dataset,'true'))
   for key,value in prepared:store.db.execute('insert or replace into settings values(?,?)',(key,json.dumps(value,ensure_ascii=False)))

def apply_to_assembly(app,record,segments,asset):
 descriptor=describe(app.store,record,segments);groups={g['slot_order']:g for g in descriptor['groups']}
 # Default groups introduce no processing or extra mix pass.
 if not any(g['effects'] or g['section_effects'] or g['candidate_overrides'] or g['color_mode']!='theme' for g in groups.values()):
  if descriptor['revision']:
   asset={**asset,'replacement_groups':descriptor};app.store.add_asset(asset)
  return asset
 layers=copy.deepcopy(asset['layers']);needs_mix=False
 for layer in layers:
  group=groups.get(layer['slot_order'])
  if group is None:continue
  layer['replacement_group_id']=group['id'];layer['replacement_group_color']={'mode':group['color_mode'],'color':group['color']}
  if layer.get('kind')=='audio':
   layer['effects']=effective_effects(group,layer.get('asset_id'))
   layer['group_section_effects']=effective_section_effects(group,layer.get('asset_id'));layer['group_section_effects_id']=group['id']
   needs_mix|=any(n['enabled'] for n in layer['effects']+layer['group_section_effects'])
 asset={**asset,'layers':layers,'replacement_groups':descriptor};app.store.add_asset(asset)
 return app._layers_render({'asset_id':asset['id'],'layers':layers},lambda _:None) if needs_mix else asset

def arrangement_group_snapshot(record,asset):
 """Settings embodied by this immutable timeline, excluding edit counters."""
 snapshot=asset.get('replacement_groups')
 if snapshot is not None:return [validate_group(group) for group in snapshot['groups']]
 segments=parse_record(record,asset.get('player',{}))['segments']
 return [default_group(record,s) for s in segments if s['type']!='fixed' and not s.get('speech_excluded')]

def arrangement_groups_current(app,record,asset):
 current=describe(app.store,record,parse_record(record,asset.get('player',{}))['segments'])['groups']
 # Undo restores identical settings under a newer catalogue revision. Comparing
 # counters alone would reject an otherwise exact restored arrangement.
 return {g['id']:g for g in current}=={g['id']:g for g in arrangement_group_snapshot(record,asset)}

def refresh_arrangement(app,base,operation):
 """Refresh one occurrence without replacing the user's edited timeline.

 Qwen's draft supplied the iteration outline; record lookup, revision checks,
 slot ownership and immutable metadata were corrected during integration.
 """
 if type(operation.get('slot_order')) is not int or type(operation.get('expected_revision')) is not int:
  raise ValueError('Choose a saved replacement group revision and occurrence.')
 from clip_ownership import current_record
 dataset,index=base['record_id'].rsplit(':',1);record=current_record(app.store,dataset,int(index))
 if base.get('source_revision')!=record['revision'] or base.get('source_metadata_revision')!=record.get('metadata_revision'):
  raise ValueError('The corpus changed after this arrangement. Review it before applying group settings.')
 catalog=load(app.store,record)
 if catalog['revision']!=operation['expected_revision']:
  raise ValueError('Replacement group settings changed. Reopen the group before rebuilding.')
 descriptor=describe(app.store,record,parse_record(record,base.get('player',{}))['segments'],catalog)
 group=next((g for g in descriptor['groups'] if g['slot_order']==operation['slot_order']),None)
 if group is None:raise ValueError('Choose a replacement occurrence in this dialogue.')
 layers=copy.deepcopy(base['layers']);matched=False
 for layer in layers:
  if layer.get('slot_order')!=group['slot_order'] or layer.get('slot',{}).get('type') in (None,'fixed') or layer.get('clip_role'):continue
  matched=True;layer['replacement_group_id']=group['id']
  layer['replacement_group_color']={'mode':group['color_mode'],'color':group['color']}
  layer['effects']=effective_effects(group,layer.get('asset_id')) if layer.get('kind')=='audio' else []
  post=effective_section_effects(group,layer.get('asset_id')) if layer.get('kind')=='audio' else []
  owned=[stage for stage in layer.get('section_history',[]) if stage.get('group_id')==group['id']]
  if owned:
   for stage in owned:stage['group_effects']=copy.deepcopy(post)
   layer.pop('group_section_effects',None);layer.pop('group_section_effects_id',None)
  else:
   layer['group_section_effects']=post;layer['group_section_effects_id']=group['id']
 if not matched:raise ValueError('This arrangement has no clips for the selected occurrence.')
 descriptor['groups']=[copy.deepcopy(group) if g['id']==group['id'] else g for g in arrangement_group_snapshot(record,base)]
 return layers,copy.deepcopy(base['tracks']),descriptor
