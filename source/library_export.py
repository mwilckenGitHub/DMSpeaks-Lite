"""Build owned, portable audio libraries with source-shaped JSON and ordered manifests."""
from engine_limits import MAX_SEGMENTS, silence_duration
import copy,csv,hashlib,json,shutil,sqlite3,uuid,zipfile
from pathlib import Path
from performance_plan import parse_record
from stage_directions import stage_format
from model import Store,segment_audio_identity,segment_audio_matches
from delivery_timing import measure_delivery
from content_review import is_delivery_eligible
from library_manifest import sequence_fields,assembly_policy
from library_stream import write_record,dialogue_key,FORMAT as STREAM_FORMAT,VERSION as STREAM_VERSION
from library_paths import clip_path,reference_path,DEFAULT_TEMPLATE,GENERIC_TEMPLATE,NARRATION_TEMPLATE,safe_component
from clip_ownership import belongs_to_record
SEQUENCE_KEYS=['layer_id','track_id','after_layer_id','anchor_start_sample','gap_samples','overlap_samples','fade_in_samples','fade_out_samples','missing_policy','fallback_silence_ms','value_match']
TIMING_KEYS=['audio_sample_count','audio_sample_rate','audio_channels','audio_seconds','audio_container_seconds','audio_codec','audio_duration_basis','audio_original_sample_count','audio_original_sample_rate','audio_original_seconds','audio_recorded_seconds']
COLUMNS=['clip_role','narrator_label','source_dialogue_id','source_event_id','corpus_id','dialogue_index','dialogue_id','quest_id','npc_id','npc_name','npc_voice_profile','dialogue_stage','dialogue_source_revision','dialogue_metadata_revision','dialogue_source_text_field','dialogue_stage_direction_format','segment_order','segment_type','segment_placeholder','segment_literal_suffix','segment_model','segment_performance_model','clip_id','clip_variant','clip_reference_id','clip_generation_key','clip_processing_json','player_value','audio_path','audio_metadata_path','audio_sha256',*TIMING_KEYS,*['segment_'+key for key in SEQUENCE_KEYS],'export_status']
def file_hash(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def folder(kind,record):
 if kind=='name' or record['all_metadata'].get('variables.'+kind+'.semantic_role')=='person_name':return 'Names'
 return {'fixed':'Fixed','race':'Race','class':'Class','gender':'Gender'}.get(kind,'Variables-'+safe_component(kind))
def build_library(app,data,status,*,allow_structured_entries=False):
 variants=data.get('variants')
 if variants is not None and (not isinstance(variants,list) or not variants or len(variants)>64 or any(not isinstance(v,str) or not v for v in variants) or len(set(variants))!=len(variants)):raise ValueError('Choose a nonempty list of unique clip variants.')
 silence_ms=data.get('silence_ms',200)
 silence_ms=silence_duration(silence_ms)
 dataset=data['dataset'];source=app.store.load(dataset);drafts=app.store.draft_map(dataset);source_revision=source.get('overlay_revision',0);mode=data.get('mode','both')
 from corpus_structure_snapshot import read as read_entry_structure
 structure_snapshot=read_entry_structure(app.store,dataset)
 if structure_snapshot is not None and not allow_structured_entries:
  raise ValueError('Audio library export does not yet support edited entry structure. Export the project to preserve its entries and audio.')
 structured_export=None;output_source=source;slot_order=None;structure_revision=None
 if structure_snapshot is not None:
  from corpus_structure_commands import read_state
  initial_structure=read_state(app.store,dataset)
  if initial_structure['snapshot']!=structure_snapshot:raise ValueError('The corpus changed during export. Retry with the current version.')
  structure_revision=initial_structure['revision']
  from corpus_structure_export import export as export_structured_corpus
  from model import decode_corpus
  structured_export=export_structured_corpus(app.store,dataset)
  if structured_export['structure_revision']!=structure_revision:raise ValueError('The corpus changed during export. Retry with the current version.')
  output_source=decode_corpus(structured_export['data'],structured_export['name'])
  if (len(structured_export['slot_map'])!=len(output_source['rows']) or
      any(item['output_index']!=position for position,item in enumerate(structured_export['slot_map']))):
   raise ValueError('The exported corpus entry mapping is inconsistent.')
  slot_order=[item['source_slot'] for item in structured_export['slot_map']]
 from clip_groups import export_config,describe
 group_config=export_config(app.store,dataset);group_catalogs={e['index']:{k:v for k,v in e.items() if k!='index'} for e in group_config['dialogues']}
 if data.get('scope','all') not in {'current','all'}:raise ValueError('Choose current dialogue or entire corpus.')
 if mode not in {'both','json','csv','jsonl'}:raise ValueError('Choose JSON, CSV, both, or streamed JSONL.')
 complete_options=data.get('complete_dialogues')
 if complete_options is not None:
  from complete_dialogue_export import validate_options
  validate_options(complete_options)
 narrator_only=isinstance(source['root'],dict) and source['root'].get('narration_policy',{}).get('scope')=='visual_narration_only'
 template=data.get('template') or (NARRATION_TEMPLATE if narrator_only else DEFAULT_TEMPLATE if source['mapping'].get('quest_id') else GENERIC_TEMPLATE)
 clip_path(template,{k:('example' if k!='segment_order' else 1) for k in __import__('library_paths').ALLOWED_FIELDS})
 visible_slots=slot_order if slot_order is not None else range(len(source['rows']))
 indices=visible_slots
 if data.get('scope')=='current':
  selected=int(data['index'])
  if selected not in visible_slots:raise ValueError('Unknown dialogue index.')
  indices=[selected]
 ident=uuid.uuid4().hex;jobdir=app.folder/'exports'/ident;root=jobdir/'library';root.mkdir(parents=True)
 index=sqlite3.connect(jobdir/'index.sqlite');index.executescript('create table clips(line text,npc text,revision text,voice text,slot integer,metadata text);create index matching on clips(line,npc,revision,voice,slot);create table paths(path text primary key,clip text);create table annotated(record integer primary key,position integer unique,value text);create table used(clip text primary key);create table delivery(clip text primary key,timing text);')
 success=False;selected_dialogue=None
 stats={'dialogues':0,'clips':0,'missing_segments':0,'unmatched_or_stale_clips':0,'timing_verified_clips':0,'reference_sources':0,'copied_reference_sources':0,'unavailable_reference_sources':0};indexed=0
 try:
  # Disk-backed index avoids retaining word timings for millions of assets in RAM.
  with app.store.lock:maximum=app.store.db.execute('select coalesce(max(rowid),0) from assets').fetchone()[0]
  offset=0
  while offset<maximum:
   with app.store.lock:rows=app.store.db.execute('select rowid,metadata from assets where rowid>? and rowid<=? order by rowid limit 250',(offset,maximum)).fetchall()
   if not rows:break
   for rowid,raw in rows:
    offset=rowid;a=json.loads(raw)
    if not is_delivery_eligible(a) or a.get('kind')!='piece' or a.get('dataset') not in (None,'',dataset) or variants is not None and a.get('variant','default') not in variants:continue
    index.execute('insert into clips values(?,?,?,?,?,?)',(str(a.get('line_id','')),str(a.get('npc_id') if a.get('npc_id') is not None else ''),a.get('source_revision',''),str(a.get('voice') or ''),a.get('slot_order'),raw));indexed+=1
   index.commit();status(f'Indexing existing clips: {indexed}')
  original=app.store.folder/(dataset+'.original')
  with original.open('rb') as original_file:raw_start=original_file.read(2)
  original_path='corpus/source'+('.csv' if source['kind']=='csv' else '.json')+('.gz' if raw_start==b'\x1f\x8b' else '')
  (root/'corpus').mkdir();shutil.copyfile(original,root/original_path)
  (root/'corpus/drafts.json').write_text(json.dumps(drafts,ensure_ascii=False),encoding='utf-8')
  extension='_voxweave_audio';n=1
  while any(extension in row for row in source['rows']):n+=1;extension='_voxweave_audio_'+str(n)
  from audio_sources import registry,embed
  sources=registry(app.store,dataset);registered={ref['id']:ref for ref in app.store.setting('references',[]) or []}
  for descriptor in sources['sources']:
   from portable_references import local_path
   stats['reference_sources']+=1;ref=registered.get(descriptor['id'])
   # Registered overrides retain their chosen file. Corpus defaults can instead
   # resolve a portable-project-owned copy, without changing the original URI.
   path=Path(ref.get('path','')) if ref else local_path(app,dataset,descriptor)
   if path and path.is_file():
    relative=reference_path(descriptor['id'],path)
    try:index.execute('insert into paths values(?,?)',(relative.casefold(),'reference:'+descriptor['id']))
    except sqlite3.IntegrityError as exc:raise ValueError('Two references resolve to the same output path.') from exc
    target=root/relative;target.parent.mkdir(exist_ok=True);shutil.copyfile(path,target)
    descriptor['origin_uri']=descriptor.get('uri','');descriptor['uri']=relative;descriptor['uri_kind']='relative';descriptor['audio_sha256']=file_hash(target);descriptor['export_status']='copied';descriptor.pop('export_issue',None);stats['copied_reference_sources']+=1
   else:
    descriptor['export_status']='unavailable';descriptor['export_issue']='Audio was not bundled. Import an accessible local copy before transferring this reference.';stats['unavailable_reference_sources']+=1
  (root/'corpus/audio-sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding='utf-8')
  (root/'corpus/replacement-groups.json').write_text(json.dumps(group_config,ensure_ascii=False,indent=2),encoding='utf-8')
  corpus_meta={'id':dataset,'name':source['name'],'source_format':source['kind'],'source_container':source['container'],'mapping':source['mapping'],'original_path':original_path,'original_sha256':file_hash(original),'audio_extension_key':extension,'audio_sources_path':'corpus/audio-sources.json','replacement_groups_path':'corpus/replacement-groups.json','sequence_policy_path':'sequence-policy.json','arrangements_path':'arrangements.json'}
  if structured_export is not None:corpus_meta['entry_slot_map']=structured_export['slot_map']
  if complete_options is not None:corpus_meta['complete_dialogues_path']='complete-dialogues.json'
  shutil.copyfile(Path(__file__).resolve().parent/'formats/audio-library-sequence-0.1.schema.json',root/'sequence-schema.json')
  (root/'sequence-policy.json').write_text(json.dumps(assembly_policy(),ensure_ascii=False,indent=2),encoding='utf-8')
  jf=(root/'manifest.json').open('w',encoding='utf-8') if mode in {'json','both'} else None
  cf=(root/'manifest.csv').open('w',encoding='utf-8-sig',newline='') if mode in {'csv','both'} else None
  lf=(root/'manifest.jsonl').open('w',encoding='utf-8') if mode=='jsonl' else None
  stream_clips=0
  writer=csv.DictWriter(cf,fieldnames=COLUMNS,lineterminator='\n') if cf else None
  if writer:writer.writeheader()
  if jf:jf.write('{"format":"voxweave-audio-library","version":"0.1","corpus":'+json.dumps(corpus_meta,ensure_ascii=False)+',"dialogues":[')
  if lf:write_record(lf,dict(record='header',format=STREAM_FORMAT,version=STREAM_VERSION,corpus=corpus_meta))
  first=True
  try:
   for i in indices:
    record=app.store.record_structure(dataset,i) if structured_export is not None else app.store.record(dataset,i)
    if data.get('scope')=='current':
     title=str(record['metadata'].get('quest_title') or ' '.join(record['text'].split()))
     selected_dialogue=title if len(title)<=100 else title[:99]+'…'
    if structured_export is not None and record['structure_revision']!=structure_revision:raise ValueError('The corpus changed during export. Retry with the current version.')
    if record.get('corpus_revision',0)!=source_revision:raise ValueError('Corpus metadata changed during export. Retry with the current version.')
    meta=record['metadata'];segments=parse_record(record,{},data)['segments'];sequence=[]
    group_state=describe(app.store,record,segments,group_catalogs.get(i,{'revision':0,'dialogue_id':record['id'],'groups':[]}));groups={g['slot_order']:g for g in group_state['groups']}
    if len(segments)>MAX_SEGMENTS:raise ValueError('Dialogue '+str(record['id'])+f' exceeds the current runtime capacity of {MAX_SEGMENTS} segments.')
    base={'corpus_id':dataset,'dialogue_index':i,'dialogue_id':record['id'],'quest_id':meta.get('quest_id') or 'unassigned','npc_id':meta.get('npc_id') if meta.get('npc_id') is not None else 'unknown','npc_name':meta.get('npc_name',''),'npc_voice_profile':meta.get('npc_voice_profile',''),'dialogue_stage':meta.get('dialogue_stage') or 'dialogue','dialogue_source_revision':record['revision'],'dialogue_metadata_revision':record.get('metadata_revision',''),'dialogue_source_text_field':record.get('source_text_field'),'dialogue_stage_direction_format':stage_format(record,data)}
    base.update(clip_role=record['all_metadata'].get('metadata.clip_role','dialogue'),narrator_label=record['all_metadata'].get('metadata.narrator_label',''),source_dialogue_id=record['all_metadata'].get('metadata.source_link.dialogue_id',record['id']),source_event_id=record['all_metadata'].get('metadata.source_link.event_id',''))
    lookup=(str(record['id']),str(meta.get('npc_id') if meta.get('npc_id') is not None else ''),record['revision'],str(meta.get('npc_voice_profile') or ''))
    stream_key=dialogue_key(base)
    if lf:write_record(lf,dict(record='dialogue',key=stream_key,entry={**base,'assembly':assembly_policy(),'source_text':record['text'],'source_metadata':record['all_metadata'],'replacement_groups':group_state}))
    if data.get('empty_folders',True) and base['clip_role']=='visual_narration':
     (root/'Narration'/safe_component(base['source_dialogue_id'])/safe_component(base['narrator_label'])/safe_component(base['source_event_id'])).mkdir(parents=True,exist_ok=True)
    elif data.get('empty_folders',True):
     prefix='Quest Speech/'+safe_component(base['quest_id']) if source['mapping'].get('quest_id') else 'Speech/'+safe_component(dataset)+'/'+safe_component(record['id'])
     for category in ['Fixed','Gender','Race','Class','Names']:(root/prefix/category).mkdir(parents=True,exist_ok=True)
    previous=None
    for segment in segments:
     sequencing=sequence_fields(segment,previous)
     if segment['type']!='fixed':sequencing['fallback_silence_ms']=silence_ms
     previous=sequencing['layer_id'];csv_sequence={'segment_'+key:sequencing[key] for key in SEQUENCE_KEYS};csv_sequence.update(segment_literal_suffix=segment.get('literal_suffix',''),segment_model=segment.get('segment_model','legacy'),segment_performance_model=segment.get('performance_model',''))
     descriptor={**sequencing,**segment_audio_identity(segment),'order':segment['order'],'type':segment['type'],'placeholder':segment['placeholder'],'source_text':segment['source_text']}
     if lf:write_record(lf,dict(record='segment',key=stream_key,segment=descriptor))
     options=[];option_count=0
     matches=(json.loads(row[0]) for row in index.execute('select metadata from clips where line=? and npc=? and revision=? and voice=? and slot=? order by rowid desc',(*lookup,segment['order'])))
     eligible=(a for a in matches if belongs_to_record(a,record) and a.get('slot_order')==segment['order'] and a.get('slot_type',segment['type'])==segment['type'] and segment_audio_matches(a,segment) and (a.get('source_metadata_revision')==record.get('metadata_revision') if a.get('source_metadata_revision') is not None else not record.get('metadata_changed',False)))
     for a in eligible:
      values={**base,'segment_folder':folder(segment['type'],record),'player_value':a.get('value') if segment['type']!='fixed' else '', 'clip_variant':a.get('variant','default'),'segment_order':segment['order'],'clip_id':a['id'],'clip_reference_id':a.get('reference_id','')}
      relative=clip_path(template,values);sidecar=str(Path(relative).with_suffix('.json')).replace('\\','/')
      target=root/relative
      prior=index.execute('select clip from paths where path=?',(relative.casefold(),)).fetchone()
      if prior and prior[0]!=a['id']:raise ValueError('Two clips resolve to the same output path.')
      if not prior:
       try:index.execute('insert into paths values(?,?)',(relative.casefold(),a['id']))
       except sqlite3.IntegrityError as exc:raise ValueError('Template produces a duplicate output path: '+relative) from exc
       path=Path(a['path'])
       if not path.is_file():raise ValueError('Missing audio for clip '+a['id']+'. Render it again before exporting.')
       from group_delivery import copy_processed_clip
       processing=copy_processed_clip(path,target,groups.get(segment['order']),a['id'])
       if not is_delivery_eligible(a):raise ValueError('Clip review or source audio changed during export. Review the current recording before rebuilding the library.')
       status(f'Measuring delivery audio: clip {stats["clips"]+1}')
       timing=measure_delivery(target);timing.update(audio_original_sample_count=a.get('original_sample_count'),audio_original_sample_rate=a.get('original_sample_rate'),audio_original_seconds=a.get('original_seconds'),audio_recorded_seconds=a.get('seconds'))
       timing['clip_processing']=processing
       index.execute('insert or replace into delivery values(?,?)',(a['id'],json.dumps(timing)))
       metadata={k:a.get(k) for k in ['id','kind','slot_order','slot_type','value','variant','reference_id','generation_key','source_revision','source_metadata_revision','original_sample_count','original_sample_rate','original_seconds','sample_rate','channels','seconds','timing_source','alignment','words','source_start_sample','source_end_sample','boundary_method','content_review','content_review_revision','isolation_review_required','insert_expected_text','source_alignment_sha256','native_edit_provenance']}
       metadata.update(segment_audio_identity(segment));metadata.update(clip_processing=processing,delivery_word_times_exceed_duration=any(w.get('end',0)>timing['audio_seconds'] for w in (a.get('words') or [])),delivery=timing,source_record_id=record['id'],source_metadata=record['all_metadata'])
       metadata['delivery_derivation']={'format':'dmspeaks-reviewed-delivery.1','source_sha256':processing['source_sha256'],'audio_sha256':file_hash(target),'reviewed_master_sha256':(a.get('content_review') or {}).get('media_sha256'),'review_scope':'original master before delivery encoding and authored effects'}
       (root/sidecar).write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8');stats['clips']+=1;stats['timing_verified_clips']+=1
      index.execute('insert or ignore into used values(?)',(a['id'],));digest=file_hash(target)
      timing=json.loads(index.execute('select timing from delivery where clip=?',(a['id'],)).fetchone()[0])
      item={'clip_id':a['id'],'clip_variant':a.get('variant','default'),'clip_reference_id':a.get('reference_id',''),'clip_generation_key':a.get('generation_key',''),'player_value':a.get('value') if segment['type']!='fixed' else None,'audio_path':relative,'audio_metadata_path':sidecar,'audio_sha256':digest,**timing,'export_status':'available'};option_count+=1
      if lf:write_record(lf,dict(record='clip',key=stream_key,order=segment['order'],clip=item));stream_clips+=1
      else:options.append(item)
      if writer:writer.writerow({**base,'segment_order':segment['order'],'segment_type':segment['type'],'segment_placeholder':segment['placeholder'],**csv_sequence,**{k:'' if v is None else v for k,v in item.items() if k!='clip_processing'},'clip_processing_json':json.dumps(item['clip_processing'],ensure_ascii=False)})
     if not option_count:
      if sequencing['missing_policy']!='skip':stats['missing_segments']+=1
      if writer:writer.writerow({**base,'segment_order':segment['order'],'segment_type':segment['type'],'segment_placeholder':segment['placeholder'],**csv_sequence,'export_status':'direction' if sequencing['missing_policy']=='skip' else 'missing'})
     sequence.append({**descriptor,'clips':options,**({'candidate_count':option_count} if lf else {}),'missing':not bool(option_count) and sequencing['missing_policy']!='skip','export_status':'available' if option_count else 'direction' if sequencing['missing_policy']=='skip' else 'missing'})
    entry={**base,'assembly':assembly_policy(),'source_text':record['text'],'source_metadata':record['all_metadata'],'replacement_groups':group_state,'segments':sequence}
    if jf:jf.write(('' if first else ',')+json.dumps(entry,ensure_ascii=False))
    stream_reference={'streamed_catalog':{'path':'manifest.jsonl','index':stats['dialogues'],'key':stream_key}} if lf else {}
    index.execute('insert into annotated values(?,?,?)',(i,stats['dialogues'],json.dumps({**base,'source_index':i,'source_metadata':record['all_metadata'],'active_source_text':record['text'],'dialogue_title':meta.get('quest_title'),'assembly':assembly_policy(),'replacement_groups':group_state,'segments':sequence,**stream_reference},ensure_ascii=False)));first=False;stats['dialogues']+=1
    if stats['dialogues']%100==0:status(f'Building library: {stats["dialogues"]} dialogues, {stats["clips"]} clips')
   stats['unmatched_or_stale_clips']=indexed-index.execute('select count(*) from used').fetchone()[0]
   if jf:jf.write('],"stats":'+json.dumps(stats)+',"path_template":'+json.dumps(template)+'}')
   if lf:write_record(lf,dict(record='end',dialogues=stats['dialogues'],clips=stream_clips,stats=stats))
  finally:
   if jf:jf.close()
   if cf:cf.close()
   if lf:lf.close()
  complete=None
  if complete_options is not None:
   from complete_dialogue_export import export_complete_dialogues
   complete=export_complete_dialogues(root,index,complete_options,silence_ms,status)
   if type(data.get('index')) is int:
    current=index.execute('select value from annotated where record=?',(data['index'],)).fetchone()
    if current:complete['current']=json.loads(current[0]).get('complete_dialogue')
  # Keep the source root/container and unselected records intact, including their graph references.
  if app.store.load(dataset).get('overlay_revision',0)!=source_revision or app.store.draft_map(dataset)!=drafts or export_config(app.store,dataset)!=group_config:raise ValueError('The corpus or group settings changed during export. Retry with the current version.')
  enriched_root=dict(output_source['root']) if isinstance(output_source['root'],dict) else []
  from clip_group_corpus import embed as embed_groups
  if isinstance(enriched_root,dict):
   embed(enriched_root,sources);embed_groups(enriched_root,output_source['clip_group_config'] if structured_export is not None else group_config,'json',output_source['container'])
  with (root/'corpus/with-audio.json').open('w',encoding='utf-8') as enriched:
   if isinstance(output_source['root'],dict):
    enriched.write('{');properties=[json.dumps(k)+':'+json.dumps(v,ensure_ascii=False) for k,v in enriched_root.items() if k!=output_source['container']];enriched.write(','.join(properties)+(',' if properties else '')+json.dumps(output_source['container'])+':[')
   else:enriched.write('[')
   for i,raw in enumerate(output_source['rows']):
    slot=slot_order[i] if slot_order is not None else i
    row=copy.deepcopy(raw) if structured_export is not None or i in drafts else dict(raw)
    if structured_export is None and i in drafts:Store._apply_export_text(source,row,drafts[i])
    if i==0 and not isinstance(output_source['root'],dict):
     embed([row],sources)
     # This array is streamed; validate against the complete source row count.
     group_copy=[row]+[{} for _ in range(len(output_source['rows'])-1)]
     embed_groups(group_copy,output_source['clip_group_config'] if structured_export is not None else group_config)
    annotation=index.execute('select value from annotated where record=?',(slot,)).fetchone()
    if annotation:row[extension]=json.loads(annotation[0])
    enriched.write((',' if i else '')+json.dumps(row,ensure_ascii=False))
   enriched.write(']'+('}' if isinstance(output_source['root'],dict) else ''))
  (root/'README.txt').write_text('DMSpeaks audio library\nPaths are relative to this folder. No player names are invented: missing segments have no clip path. JSON/CSV manifest rows preserve source identifiers and segment order. corpus/with-audio.json retains the imported root/record fields and adds '+extension+'. Original bytes and draft overlay are included separately. An assembly adapter is required; this is not a claim that an unmodified game addon reads this draft format. Timing sidecars retain estimated/native provenance. audio_seconds and audio_sample_count measure the decoded delivered file; original and recorded lengths are separate. sequence-policy.json defines the shared sample clock and after-previous-clip dependencies, including across tracks. Missing variable clips remain silent by policy; missing fixed speech is an error. Select a matching reference/performance group before choosing variable values. Candidate files contain their effective occurrence-group effects and individual override, recorded in clip_processing. Existing Vorbis without active effects is copied unchanged; non-Vorbis input is converted whole to Vorbis. Do not reapply baked effects. Custom arrangement-only layer edits and mixed previews remain separate. Optional edited arrangements are listed in arrangements.json, with separately baked clips, JSON/CSV timing and a lossless preview under Arrangements/. With active continuous track effects, arrangement version 0.2 provides playback_streams: play each stream once at its position, including lossless track stems. Bus member rows are editing metadata without their own delivery files; do not play them a second time. Without active buses, version 0.1 retains separate clips. Delivery files already contain their selected processing; do not apply it twice. A host adapter must implement selection/playback and verify its decoder; these timings do not establish gapless or sample-accurate playback in WoW.\n',encoding='utf-8')
  with (root/'README.txt').open('a',encoding='utf-8') as readme:readme.write('\nReference samples\nAvailable registered and corpus-declared local samples, including restored project copies, are copied unchanged under References/. corpus/audio-sources.json records their relative paths, SHA-256 digests, original locators and export_status. Unavailable or remote samples retain their declarations with export_status=unavailable; they are not fetched automatically. Review the reference counts and issues before transferring the library. Voice identity and transcript quality still require listening review.\n')
  if complete:
   with (root/'README.txt').open('a',encoding='utf-8') as readme:readme.write('\nComplete dialogue recordings\ncomplete-dialogues.json and complete-dialogues.csv describe one explicit test-character profile. audio_path and audio_metadata_path are exact relative paths under the selected finished-file template. Only available entries advertise files; unavailable fixed speech is reported without a path. Omitted variable clips follow the selected silence/skip policy. Source placeholders and constituent clips remain available. Timings in sidecars retain their provenance; final file durations are measured after Ogg encoding. A target pack builder must map dialogue identities and enforce character/profile matching. This library does not itself contain a generated Spoken addon or establish in-game playback.\n')
  arrangements=[]
  if data.get('arrangement_asset_id'):
   from arrangement_export import export_arrangement
   current=app.store.record_structure(dataset,int(data['index'])) if structured_export is not None else app.store.record(dataset,int(data['index']))
   if current['index'] not in indices:raise ValueError('Include the arrangement’s dialogue in this export.')
   arrangements.append(export_arrangement(app,root,app.store.asset(data['arrangement_asset_id']),current,status))
   with (root/'README.txt').open('a',encoding='utf-8') as readme:
    readme.write('\nEdited mix\nThe authored_mix entry points to the complete edited-mix.ogg, with the editor’s positions and overlaps baked in. Play it once for the saved dialogue and selected values, or use the positioned streams for separate layer playback. The two are alternative playback paths.\n')
  (root/'arrangements.json').write_text(json.dumps(arrangements,ensure_ascii=False,indent=2),encoding='utf-8')
  from background_library import FORMAT as BACKGROUND_FORMAT,VERSION as BACKGROUND_VERSION
  (root/'backgrounds.json').write_text(json.dumps(dict(format=BACKGROUND_FORMAT,version=BACKGROUND_VERSION,
      choices=[choice for arrangement in arrangements for choice in arrangement.get('background_choices',[])]),ensure_ascii=False,indent=2),encoding='utf-8')
  if app.store.load(dataset).get('overlay_revision',0)!=source_revision or app.store.draft_map(dataset)!=drafts or read_entry_structure(app.store,dataset)!=structure_snapshot or (structured_export is not None and read_state(app.store,dataset)['revision']!=structure_revision):raise ValueError('The corpus changed during export. Retry with the current version.')
  index.commit()
  status('Packing portable library…');archive=jobdir/'voxweave-audio-library.zip'
  with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
   for path in sorted(root.rglob('*')):z.write(path,path.relative_to(root).as_posix()+('/' if path.is_dir() else ''))
  success=True
  reference_report=[{'id':s['id'],'name':s.get('name') or s['id'],'status':s['export_status'],'output_path':s['uri'] if s['export_status']=='copied' else '', 'reason':s.get('export_issue','')} for s in sources['sources']]
  return {'export_id':ident,'folder':str(root),'download':'export/library/'+ident,'stats':stats,'reference_report':reference_report,'manifest_formats':mode,'source_audio_extension':extension,'arrangements':arrangements,'complete_dialogues':complete,'selected_dialogue':selected_dialogue}
 finally:
  index.close()
  if not success:shutil.rmtree(jobdir,ignore_errors=True)
