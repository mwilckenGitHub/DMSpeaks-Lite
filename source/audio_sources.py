"""Corpus-owned reference metadata, independent of a game or speech provider."""
import copy,json
from pathlib import Path
TEXT_FIELDS={'speaker_name','locale','description','origin_url','license','attribution'}
EXTENSION='_voxweave_audio_sources'
def validate_metadata(value):
 if value is None:return {}
 if not isinstance(value,dict):raise ValueError('Audio source metadata must be an object.')
 result=copy.deepcopy(value)
 for key in TEXT_FIELDS:
  if key in result and (not isinstance(result[key],str) or len(result[key])>10000):raise ValueError('Invalid audio source '+key+'.')
 if 'tags' in result and (not isinstance(result['tags'],list) or len(result['tags'])>100 or any(not isinstance(t,str) or len(t)>100 for t in result['tags'])):raise ValueError('Audio source tags must be a list of short strings.')
 if 'custom' in result and not isinstance(result['custom'],dict):raise ValueError('Custom audio source metadata must be an object.')
 if len(json.dumps(result,ensure_ascii=False,allow_nan=False))>100000:raise ValueError('Audio source metadata is too large.')
 return result

def capture_workspace(store,workspace,*,commit=True):
 dataset=workspace.get('dataset')
 if not dataset:return
 store.load(dataset)
 key='corpus_audio_sources:'+dataset;current=store.setting(key,{}) or {}
 current['voice_bindings']={provider:{speaker:copy.deepcopy(binding) for speaker,binding in bindings.items() if speaker.startswith(dataset+':')} for provider,bindings in workspace.get('voice_bindings',{}).items()}
 if commit:store.set_setting(key,current)
 else:store.db.execute('insert or replace into settings values(?,?)',(key,json.dumps(current,ensure_ascii=False)))

def attach_source(store,dataset,reference_id):
 store.load(dataset);key='corpus_audio_sources:'+dataset;current=store.setting(key,{}) or {};current['reference_ids']=sorted(set(current.get('reference_ids',[]))|{reference_id});store.set_setting(key,current)

def embedded(data):
 root=data['root']
 if isinstance(root,dict):
  if root.get('format')=='open-speech-corpus' and 'audio_sources' in root:
   value={'format':'voxweave-audio-sources','version':'0.1','sources':root['audio_sources'],'voice_bindings':root.get('render_bindings',{}),'dataset_id':root.get('audio_sources_dataset_id')}
  else:value=root.get(EXTENSION)
 else:value=root[0].get(EXTENSION+'_json' if data['kind']=='csv' else EXTENSION) if root else None
 if isinstance(value,str):
  try:value=json.loads(value)
  except ValueError:return {}
 if not isinstance(value,dict) or value.get('format')!='voxweave-audio-sources':return {}
 sources=value.get('sources',[]);bindings=value.get('voice_bindings',{})
 if not isinstance(sources,list) or len(sources)>10000 or not isinstance(bindings,dict):raise ValueError('Invalid corpus audio source registry.')
 seen=set()
 for source in sources:
  if not isinstance(source,dict) or not isinstance(source.get('id'),str) or not source['id'] or source['id'] in seen:raise ValueError('Audio source IDs must be unique non-empty strings.')
  seen.add(source['id'])
  for key in ['name','uri','transcript']:
   if key in source and not isinstance(source[key],str):raise ValueError('Invalid audio source '+key+'.')
  validate_metadata(source.get('source_metadata'))
  if not isinstance(source.get('voice_profile_refs',[]),list) or any(not isinstance(v,str) for v in source.get('voice_profile_refs',[])):raise ValueError('Invalid audio source voice-profile references.')
 for provider,choices in bindings.items():
  if not isinstance(choices,dict):raise ValueError('Invalid corpus voice bindings.')
  for speaker,binding in choices.items():
   if not isinstance(speaker,str) or not isinstance(binding,dict) or any(k in binding and not isinstance(binding[k],str) for k in ['reference_id','voice']):raise ValueError('Invalid corpus speaker reference override.')
 return copy.deepcopy(value)

def registry(store,dataset):
 data=store.load(dataset);saved=store.setting('corpus_audio_sources:'+dataset,{}) or {};refs=store.setting('references',[]) or []
 from model import getfield
 profiles={str(getfield(row,data['mapping'].get('npc_voice_profile')) or '') for row in data['rows']}
 if isinstance(data['root'],dict):profiles.update(str(s.get('voice_profile_ref') or '') for s in data['root'].get('speakers',[]) if isinstance(s,dict))
 prior=embedded(data)
 old_dataset=prior.get('dataset_id')
 if isinstance(old_dataset,str) and old_dataset!=dataset:
  prior['voice_bindings']={provider:{dataset+speaker[len(old_dataset):] if speaker.startswith(old_dataset+':') else speaker:binding for speaker,binding in choices.items()} for provider,choices in prior.get('voice_bindings',{}).items()}
 declared={item['id']:item for item in prior.get('sources',[])}
 ids=set(saved.get('reference_ids',[]))|set(declared);bindings=copy.deepcopy(prior.get('voice_bindings',{}));bindings.update(copy.deepcopy(saved.get('voice_bindings',{})))
 for choices in bindings.values():ids.update(v.get('reference_id') for v in choices.values() if isinstance(v,dict) and v.get('reference_id'))
 items=[]
 for ref in refs:
  if ref.get('id') not in ids and ref.get('id') not in profiles and ref.get('fish_profile') not in profiles:continue
  item={k:copy.deepcopy(ref[k]) for k in ['id','name','transcript','source_metadata','parent_reference_id','audio_metadata','source_audio_metadata','source_sha256','created_at'] if k in ref}
  item['uri']=ref.get('source_locator',{}).get('uri') or ref.get('path','');item['uri_kind']=ref.get('source_locator',{}).get('kind','local')
  item['voice_profile_refs']=sorted(set(declared.get(ref['id'],{}).get('voice_profile_refs',[]))|{p for p in profiles if p and p in (ref.get('id'),ref.get('fish_profile'))});items.append(item)
 known=declared
 for item in items:known[item['id']]={**known.get(item['id'],{}),**item}
 return {**prior,'format':'voxweave-audio-sources','version':'0.1','dataset_id':dataset,'sources':list(known.values()),'voice_bindings':bindings}

def embed(root,payload,kind='json',container=None):
 """Preserve source shape; legacy array/CSV roots use a reserved first-record field."""
 if not payload['sources'] and not any(payload['voice_bindings'].values()):return root
 if isinstance(root,dict):
  if root.get('format')=='open-speech-corpus':
   root['audio_sources']=payload['sources'];root['render_bindings']=payload['voice_bindings'];root['audio_sources_dataset_id']=payload.get('dataset_id');return root
  previous=root.get(EXTENSION)
  if previous is not None and (not isinstance(previous,dict) or previous.get('format')!='voxweave-audio-sources'):raise ValueError('The corpus already uses the reserved audio-source field. Rename it with the corpus builder before exporting references.')
  root[EXTENSION]=payload
 elif root:
  key=EXTENSION+'_json' if kind=='csv' else EXTENSION
  previous=root[0].get(key)
  if previous:
   parsed=json.loads(previous) if kind=='csv' else previous
   if not isinstance(parsed,dict) or parsed.get('format')!='voxweave-audio-sources':raise ValueError('The corpus already uses the reserved audio-source field.')
  root[0][key]=json.dumps(payload,ensure_ascii=False) if kind=='csv' else payload
  if kind=='csv':
   for row in root[1:]:row.setdefault(key,'')
 return root

def list_sources(app,dataset=None):
 """Expose declarations even when their audio has not been imported on this machine."""
 refs={ref['id']:copy.deepcopy(ref) for ref in app.store.setting('references',[]) or []}
 if dataset:
  for item in registry(app.store,dataset)['sources']:
   if item['id'] not in refs:
    refs[item['id']]={**item,'declared_only':True,'source_locator':{'uri':item.get('uri',''),'kind':item.get('uri_kind','unknown')}}
    from portable_references import local_path
    try:
     path=local_path(app,dataset,item)
     if path is not None:refs[item['id']]['path']=str(path)
    except (ValueError,OSError) as exc:refs[item['id']]['availability_issue']=str(exc)
 for ref in refs.values():ref['available']=bool(ref.get('path') and Path(ref['path']).is_file())
 return {'sources':list(refs.values())}
