"""Portable project bundles: immutable corpus, private drafts/settings, owned media."""
from engine_limits import MAX_SEGMENTS
import copy,io,json,zipfile,uuid,re,time
from contextlib import nullcontext
from pathlib import Path
MAX_EXPANDED=512_000_000
ASSET_REFERENCES=("source_take_asset_id","parent_assembly_id","source_clip_id","boundary_parent_piece_id","boundary_source_asset_id","boundary_repair_piece_id","boundary_piece_id","whole_take_piece_id")

def media_suffix(path):
 suffix=Path(path).suffix.lower()
 return suffix if re.fullmatch(r'\.[a-z0-9]{1,10}',suffix) else '.audio'

def narration_references(asset):
 policy=asset.get('visual_narration')
 if policy is None:return []
 if not isinstance(policy,dict) or not isinstance(policy.get('base_asset_id'),str) or not policy['base_asset_id'] or policy['base_asset_id']==asset.get('id'):raise ValueError('Invalid narrator base arrangement reference.')
 placements=policy.get('placements',[])
 if not isinstance(placements,list) or len(placements)>MAX_SEGMENTS or any(not isinstance(p,dict) or not isinstance(p.get('asset_id'),str) or not p['asset_id'] for p in placements):raise ValueError('Invalid narrator clip references.')
 return [policy['base_asset_id'],*[p['asset_id'] for p in placements]]

def export_project(app,dataset,*,workspace_override=None):
 buffer=io.BytesIO()
 with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
  write_project(app,dataset,archive,workspace_override=workspace_override)
 return buffer.getvalue()

def write_project(app,dataset,archive,*,workspace_override=None):
 """Write one snapshot to a ZIP or a content-comparison sink."""
 store=app.store;data=store.load(dataset);record_prefix=dataset+':'
 # Capture roots first, then include every reachable media dependency once.
 takes=[]
 with store.lock:
  for row in store.db.execute('select metadata from takes'):
   take=json.loads(row[0])
   if str(take.get('record_id','')).startswith(record_prefix):takes.append(take)
 workspace=copy.deepcopy(workspace_override if workspace_override is not None else store.setting('workspace',{}) or {})
 from corpus_structure_snapshot import read as read_entry_structure
 entry_structure=read_entry_structure(store,dataset)
 # An empty corpus has no selected entry. Undo can hide the previous selection
 # before the browser has another entry to select; do not serialize that stale
 # selection or its playback state into an otherwise valid empty project.
 if (workspace_override is None and entry_structure is not None and
     not entry_structure['state']['order'] and workspace.get('dataset')==dataset):
  workspace.pop('index',None)
  workspace.pop('asset_id',None)
  workspace.pop('selections',None)
 if workspace_override is not None:
  from workspace_state import validate_workspace
  validate_workspace(workspace)
 from project_references import ProjectReferences
 project_references=ProjectReferences(store,dataset,workspace)
 asset_map={};pending=[]
 def include(ident):
  if ident in asset_map:return
  try:asset=store.asset(ident)
  except ValueError as exc:raise ValueError(f'Missing required project audio: {ident}') from exc
  asset_map[ident]=asset;pending.append(asset)
 for asset in store.assets():
  if asset.get('kind') in {'boundary-preview','boundary-join-preview'}:continue
  if asset.get('dataset')==dataset or str(asset.get('record_id','')).startswith(record_prefix):include(asset['id'])
 for take in takes:
  project_references.include_metadata(take)
  include(take['asset_id'])
  for ident in take.get('piece_ids',[]):include(ident)
 if workspace.get('dataset')==dataset:
  for ident in [workspace.get('asset_id'),*workspace.get('selections',{}).values()]:
   if ident and ident!='silence':include(ident)
 included_reference_ids=set()
 def include_reference_assets():
  for ref in project_references.selected():
   if ref['id'] in included_reference_ids:continue
   included_reference_ids.add(ref['id'])
   if ref.get('asset_id'):include(ref['asset_id'])
 include_reference_assets()
 from clip_groups import export_config
 group_config=export_config(store,dataset)
 for entry in group_config['dialogues']:
  for group in entry['groups']:
   for ident in group['candidate_overrides']:include(ident)
 from replacement_snap import references as snap_references
 for asset in pending:
  project_references.include_metadata(asset)
  include_reference_assets()
  for ident in narration_references(asset):include(ident)
  from replacement_playback_state import references as playback_references
  for ident in playback_references(asset):include(ident)
  from contextual_choices import references as contextual_references
  for ident in contextual_references(asset):include(ident)
  for layer in asset.get('layers',[]):
   for ident in snap_references(layer):include(ident)
  for slot in asset.get('slots',[])+asset.get('layers',[]):
   if slot.get('asset_id'):include(slot['asset_id'])
   if slot.get('replacement_origin_asset_id'):include(slot['replacement_origin_asset_id'])
  for key in ASSET_REFERENCES:
   if asset.get(key):
    if key=='source_clip_id':
     try:store.asset(asset[key])
     except ValueError:continue # Exported library copies remain usable without the original library.
    include(asset[key])
 assets=list(asset_map.values())
 references=project_references.selected()
 manifest={'format':'dialogue-studio-project','version':2,'dataset':dataset,'name':data['name'],'mapping':store.original_load(dataset)['mapping'],'drafts':store.draft_map(dataset),'settings':{k:store.setting(k,default) or default for k,default in [('characters',[]),('layout',{}),('theme','system'),('workspace',{})]},'references':[],'assets':[],'takes':takes}
 manifest['settings']['workspace']=workspace
 from source_text_lock import is_locked
 manifest['source_text_locked']=is_locked(store,dataset)
 manifest['corpus_overlay']=store.overlay(dataset)
 if entry_structure is not None:
  manifest['version']=3
  manifest['corpus_entry_structure']=entry_structure
 from project_delimiter_library import read as read_project_delimiters
 manifest['project_delimiters']=read_project_delimiters(store,dataset)
 manifest['corpus_audio_sources']=store.setting('corpus_audio_sources:'+dataset,{}) or {}
 manifest['replacement_groups']=group_config
 saved_workspace=manifest['settings']['workspace']
 entry_audio={key:ident for key,ident in saved_workspace.pop('entry_audio',{}).items()
              if key.startswith(record_prefix) and asset_map.get(ident,{}).get('record_id')==key}
 if saved_workspace.get('dataset')!=dataset:
  saved_workspace=manifest['settings']['workspace']={}
 if entry_audio:
  saved_workspace.update(dataset=dataset,entry_audio=entry_audio)
 archive.writestr('original.corpus',(store.folder/(dataset+'.original')).read_bytes())
 from native_join_evidence import bundle_context
 from phoneme_alignment import for_asset as phone_evidence
 included_returns=set()
 for asset in assets:
  item={k:v for k,v in asset.items() if k!='path'};
  from data_audio_import import bundle_input
  bundle_input(archive,item,asset)
  bundle_context(archive,item,asset,store,included_returns)
  if (alignment:=phone_evidence(store,asset)) is not None:item['phoneme_alignment']=alignment
  if item.get('source_clip_id') and item['source_clip_id'] not in asset_map:item['external_source_clip_id']=item.pop('source_clip_id')
  item['media_entry']='media/'+asset['id']+media_suffix(asset['path']);manifest['assets'].append(item);archive.write(asset['path'],item['media_entry'])
 for ref in references:
  # fish_profile is a speaker-matching alias, not a connection credential.
  # Preserve it so reopening the project retains automatic voice selection.
  item={k:v for k,v in ref.items() if k!='path'}
  if not ref.get('asset_id') and ref.get('path') and Path(ref['path']).is_file():
   entry='references/'+uuid.uuid4().hex+Path(ref['path']).suffix;archive.write(ref['path'],entry);item['media_entry']=entry
  manifest['references'].append(item)
 from portable_references import write_media
 manifest['corpus_reference_media'],manifest['corpus_reference_unbundled']=write_media(app,dataset,data,archive)
 if manifest['corpus_reference_unbundled']:
  archive.writestr('REFERENCE-NOTES.txt','Some corpus samples were not bundled. Their declarations remain in the corpus.\nImport required samples before moving to another computer.\n\n'+'\n'.join(item['source_id']+': '+item['reason'] for item in manifest['corpus_reference_unbundled']))
 archive.writestr('project.json',json.dumps(manifest,ensure_ascii=False))

def restore_project(app,raw,*,allow_experimental_structure=False,source_only=False,asset_ids=None,
                    source_slot=None):
 """Restore a project, or populate a private store for source inspection.

 Source inspection can use a caller-owned ZIP. A source slot limits draft
 restoration to one entry in a fresh inspection store. The rest of the
 corpus still provides metadata and stable identities.
 """
 if isinstance(raw,zipfile.ZipFile) and not source_only:
  raise ValueError('An open project archive is only supported for source inspection.')
 if source_slot is not None and (not source_only or type(source_slot) is not int or source_slot<0):
  raise ValueError('A single source slot requires source inspection.')
 from project_preflight import preflight_project
 manifest,original,validated_media=preflight_project(raw,inspect_media=not source_only)
 incoming_structure=manifest.get('corpus_entry_structure')
 if incoming_structure is not None and not allow_experimental_structure:
  raise ValueError('This project uses an entry structure that is not yet supported by the workspace.')
 # Audio bytes have already been read, verified and probed. Only original
 # imported data inputs need access to the archive during restoration.
 needs_inputs=not source_only and any('data_import' in item for item in manifest.get('assets',[]))
 with (zipfile.ZipFile(io.BytesIO(raw)) if needs_inputs else nullcontext()) as archive:
  mapping=manifest['mapping']
  drafts=manifest.get('drafts',{})
  assets=manifest.get('assets',[]);refs=manifest.get('references',[])
  # Refuse to replace a different working corpus already open in this library.
  expected_id=manifest['dataset']
  incoming=manifest.get('corpus_overlay');existing=app.store.db.execute('select 1 from datasets where id=?',(expected_id,)).fetchone()
  if existing:
   if source_slot is not None:raise ValueError('Single-entry source inspection needs a fresh store.')
   from corpus_structure_snapshot import read as read_entry_structure
   if read_entry_structure(app.store,expected_id)!=incoming_structure:
    raise ValueError('This corpus already has a different entry structure. Restore into a separate project folder to preserve both versions.')
   current=app.store.overlay(expected_id)
   if (current is None)!=(incoming is None) or current is not None and (current['root']!=incoming['root'] or current['mapping']!=incoming['mapping']):raise ValueError('This corpus already has different working metadata. Restore into a separate project folder to preserve both versions.')
   from project_delimiter_library import read as read_project_delimiters
   if read_project_delimiters(app.store,expected_id)!=manifest['project_delimiters']:
    raise ValueError('This corpus already has a different project delimiter collection. Restore into a separate project folder to preserve both versions.')
   # A matching original and metadata overlay do not imply matching edited
   # dialogue. Never silently replace or combine the two sets of drafts.
   current_drafts=app.store.draft_map(expected_id)
   incoming_drafts={int(index):text for index,text in drafts.items()}
   for index in set(current_drafts)|set(incoming_drafts):
    record=(app.store.record_structure(expected_id,index,include_hidden=True)
            if incoming_structure is not None else app.store.record(expected_id,index))
    source=record['original_text']
    if current_drafts.get(index,source)!=incoming_drafts.get(index,source):
     raise ValueError('This corpus already has different dialogue edits. Restore into a separate project folder to preserve both versions.')
  if not source_only:
   from project_settings_merge import plan_settings_merge
   settings_plan=plan_settings_merge(app.store,manifest,validated_media)
   had_projects=bool(app.store.datasets())
  staged=app.store.stage(original,manifest['name'],preserve_tei_styles=False)
  dataset=app.store.import_staged(staged['staged_id'],mapping)['id']
  if not existing and incoming_structure is not None:
   from corpus_structure_snapshot import setting_key
   app.store.set_setting(setting_key(dataset),incoming_structure)
  if not existing:
   from project_delimiter_library import write as write_project_delimiters
   write_project_delimiters(app.store,dataset,manifest['project_delimiters'])
  if incoming and not existing:
   with app.store.lock:
    app.store.db.execute('insert into corpus_overlays values(?,?,?,?)',(dataset,json.dumps(incoming['root'],ensure_ascii=False),json.dumps(incoming['mapping']),incoming['revision']));app.store.db.commit();app.store.cache.pop(dataset,None)
  if source_slot is not None:
   selected_draft=str(source_slot)
   drafts={selected_draft:drafts[selected_draft]} if selected_draft in drafts else {}
  for index,text in drafts.items():
   slot=int(index)
   if incoming_structure is not None and slot>=incoming_structure['original_count']:
    # The archive was validated above, including hidden stable slots. Restore
    # their drafts without making a user edit or changing structure history.
    with app.store.lock:
     app.store.db.execute('insert or replace into drafts values(?,?,?,?)',(dataset,slot,text,time.time()))
     app.store.db.commit()
   else:app.store.save_draft(dataset,slot,text)
  if source_only:
   return app.store.dataset_info(dataset)
  from native_join_evidence import restore_context
  ids={};new_assets=[];by_original_id={}
  for item in assets:
   old_id=item['id'];ident=uuid.uuid4().hex;ids[old_id]=ident;entry=item['media_entry']
   path=app.audio.root/(ident+media_suffix(entry));path.write_bytes(validated_media[entry])
   asset={k:v for k,v in item.items() if k!='media_entry'};asset.update(id=ident,path=str(path))
   from data_audio_import import restore_input
   restore_input(archive,asset,app.audio.root,ident)
   restore_context(asset,validated_media,app.store.folder)
   new_assets.append(asset)
   by_original_id[old_id]=asset
  from replacement_playback_state import remap as remap_playback
  from contextual_choices import remap as remap_contextual
  from replacement_snap import remap as remap_snap
  from contextual_delivery import upgrade_portable_review
  for asset in new_assets:upgrade_portable_review(asset,by_original_id.__getitem__)
  for asset in new_assets:
   for layer in asset.get('layers',[]):remap_snap(layer,ids)
   remap_playback(asset,ids)
   remap_contextual(asset,ids)
   for slot in asset.get('slots',[])+asset.get('layers',[]):
    if slot.get('asset_id'):slot['asset_id']=ids.get(slot['asset_id'])
    if slot.get('replacement_origin_asset_id'):slot['replacement_origin_asset_id']=ids[slot['replacement_origin_asset_id']]
    if isinstance(slot.get('slot'),dict) and slot['slot'].get('asset_id'):slot['slot']['asset_id']=ids.get(slot['slot']['asset_id'])
   for key in ASSET_REFERENCES:
    if asset.get(key):asset[key]=ids.get(asset[key])
   if asset.get('visual_narration'):
    policy=asset['visual_narration'];policy['base_asset_id']=ids[policy['base_asset_id']]
    for placement in policy.get('placements',[]):placement['asset_id']=ids[placement['asset_id']]
   if asset.get('replacement_groups'):
    for group in asset['replacement_groups']['groups']:
     group['candidate_overrides']={ids[k]:v for k,v in group.get('candidate_overrides',{}).items()}
   app.store.add_asset(asset)
  for item in manifest.get('takes',[]):
   item.update(id=uuid.uuid4().hex,asset_id=ids[item['asset_id']],piece_ids=[ids[x] for x in item.get('piece_ids',[])]);app.store.add_take(item)
  restored_refs=settings_plan['references']
  for item in refs:
   if item['id'] not in settings_plan['add_reference_ids']:continue
   item=dict(item);asset_id=ids.get(item.get('asset_id'))
   if asset_id:item.update(asset_id=asset_id,path=app.store.asset(asset_id)['path'])
   elif item.get('media_entry'):
    entry=item.pop('media_entry')
    path=app.folder/('reference-'+uuid.uuid4().hex+media_suffix(entry));path.write_bytes(validated_media[entry]);item['path']=str(path)
   restored_refs.append(item)
  app.store.set_setting('characters',settings_plan['characters'])
  if not had_projects:
   for k,v in manifest.get('settings',{}).items():
    if k in {'layout','theme'} and app.store.setting(k) is None:app.store.set_setting(k,v if v is not None else {'layout':{},'theme':'system'}[k])
  from workspace_state import remap_workspace
  workspace=remap_workspace(manifest.get('settings',{}).get('workspace'),ids,dataset)
  # Library imports select the incoming corpus without discarding remembered
  # audio from other corpora already in this working library.
  prior_workspace=app.store.setting('workspace',{}) or {}
  other_entry_audio={key:ident for key,ident in prior_workspace.get('entry_audio',{}).items()
                     if not key.startswith(dataset+':')}
  if other_entry_audio:
   workspace['entry_audio']={**other_entry_audio,**workspace.get('entry_audio',{})}
  if workspace.get('character_id') in settings_plan['character_ids']:workspace['character_id']=settings_plan['character_ids'][workspace['character_id']]
  app.store.set_setting('workspace',workspace)
  app.store.set_setting('references',restored_refs)
  from clip_groups import restore_config,FORMAT
  restore_config(app.store,dataset,manifest.get('replacement_groups',{'format':FORMAT,'version':'0.1','dialogues':[]}),ids)
  source_config=manifest.get('corpus_audio_sources',{})
  if source_config:
   old=manifest.get('dataset','');config=json.loads(json.dumps(source_config))
   config['voice_bindings']={p:{dataset+k[len(old):] if k.startswith(old+':') else k:v for k,v in choices.items()} for p,choices in config.get('voice_bindings',{}).items()}
   app.store.set_setting('corpus_audio_sources:'+dataset,config)
  from portable_references import restore_media
  restore_media(app,dataset,manifest.get('corpus_reference_media',[]),validated_media)
  from source_text_lock import set_locked
  set_locked(app.store,dataset,manifest.get('source_text_locked',False))
 if asset_ids is not None:asset_ids.update(ids)
 return app.store.dataset_info(dataset)
