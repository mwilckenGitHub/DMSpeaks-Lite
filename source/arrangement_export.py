"""Bake a selected arrangement into independent delivery clips, preserving sources."""
import array,copy,json,shutil
from pathlib import Path
from audio import encode_pcm,decode,encode_preview_pcm
from delivery_timing import measure_delivery
from layered_audio import render_layers,RATE
from layer_schedule import schedule_layers
from library_paths import safe_component
from guide_bindings import delivery_guides

def export_arrangement(app,root,asset,record,status):
 from content_review import assert_audio_usable
 from replacement_playback import delivery_asset
 asset=delivery_asset(app.store,asset)
 from processing_definition import require_ordinary_rebuild
 require_ordinary_rebuild(asset,allow_track_hierarchy=True)
 assert_audio_usable(asset,app.store)
 if not asset.get('layers'):raise ValueError('Assemble or render a track preview first.')
 if asset.get('record_id')!=record['dataset']+':'+str(record['index']) or asset.get('source_revision')!=record['revision']:raise ValueError('The arrangement does not match the selected dialogue revision.')
 if (asset.get('source_metadata_revision')!=record.get('metadata_revision') if asset.get('source_metadata_revision') is not None else record.get('metadata_changed',False)):raise ValueError('The arrangement uses earlier metadata. Reassemble before exporting.')
 from clip_groups import arrangement_groups_current
 if not arrangement_groups_current(app,record,asset):raise ValueError('Replacement group settings changed. Rebuild the affected groups before exporting the edited arrangement.')
 from library_export import file_hash
 preserve_base=(asset.get('contextual_assembly') or {}).get('preserve_base') is True
 stems={};bus_stems={};authored_pcm,rendered=render_layers(asset['layers'],asset['tracks'],app.store,stems,
                                                        bus_stems=bus_stems,normalize_master=not preserve_base)
 channels=rendered.get('channels',1)
 if asset.get('contextual_delivery'):
  reviewed=decode(asset['path'],channels=channels)
  if len(reviewed)!=len(authored_pcm) or any(abs(a-b)>2**-23 for a,b in zip(reviewed,authored_pcm)):
   raise ValueError('The current mix differs from the reviewed recording. Rebuild and review it before export.')
  authored_pcm=reviewed
  del reviewed
 relative=Path('Arrangements')/safe_component(asset['id']);folder=root/relative;folder.mkdir(parents=True,exist_ok=True);layers=[];decoded={}
 status('Exporting the edited mix…')
 authored_path=folder/'edited-mix.ogg'
 encode_pcm(authored_pcm,authored_path,channels=channels)
 authored_timing=measure_delivery(authored_path)
 if (authored_timing['audio_sample_rate'],authored_timing['audio_sample_count'])!=(RATE,rendered['sample_count']):
  raise ValueError('The edited mix does not match its saved timeline clock.')
 authored_mix={**authored_timing,'audio_path':(relative/authored_path.name).as_posix(),
               'audio_sha256':file_hash(authored_path),
               'playback':'Play this complete mix once, or play the positioned streams separately.'}
 del authored_pcm
 for layer in rendered['layers']:
  status('Exporting edited layer '+str(layer['slot_order']))
  item={key:copy.deepcopy(value) for key,value in layer.items() if key!='waveform'};item.update(editor_start_sample=layer['start_sample'],editor_end_sample=layer['end_sample']);pcm=stems[layer['id']]
  if layer.get('clip_role') in {'background_music','background_ambience'}:
   from project_io import media_suffix
   source=app.store.asset(layer['asset_id']);source_relative=relative/'Sources'/(safe_component(source['id'])+media_suffix(source['path']));source_target=root/source_relative;source_target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source['path'],source_target)
   item.update(source_audio_path=source_relative.as_posix(),source_audio_sha256=file_hash(source_target),source_bytes_preserved=True)
  if layer.get('track_bus_id'):
   item.update(audio_path=None,audio_sha256=None,export_status='routed_to_track_bus',delivery_route='track_bus')
  elif pcm:
   name=f"{layer['slot_order']:02d}__{safe_component(layer['id'])}.ogg";path=folder/name;encode_pcm(pcm,path,channels=channels);timing=measure_delivery(path)
   item.update(**timing,audio_path=(relative/name).as_posix(),audio_sha256=file_hash(path),rendered_sample_count=timing['audio_sample_count'],export_status='available');decoded[item['id']]=decode(path,channels=channels)
  else:item.update(audio_path=None,audio_sha256=None,rendered_sample_count=0,export_status='skipped')
  item['processing_baked']={'playback_speed':layer.get('playback_speed',1),'source_in_sample':layer.get('source_in_sample',0),'source_out_sample':layer.get('source_out_sample'),'gain_db':layer.get('gain_db',0),'fade_in_samples':layer.get('fade_in_samples',0),'fade_out_samples':layer.get('fade_out_samples',0),'muted':bool(layer.get('muted')),'track':next(copy.deepcopy(track) for track in asset['tracks'] if track['id']==layer['track_id']),'master_gain':rendered['master_gain'],'effects':copy.deepcopy(layer.get('effects',[])),'effects_engine':layer.get('effects_engine'),'track_effects':copy.deepcopy(layer.get('track_effects',[])),'track_effect_tail_sample_count':layer.get('track_effect_tail_sample_count',0),'bypass_track_effects':layer.get('bypass_track_effects',False),'dry_rendered_sample_count':layer.get('dry_rendered_sample_count'),'effect_tail_sample_count':layer.get('effect_tail_sample_count')}
  if not layer.get('track_bus_id'):
   item['snap_points']=delivery_guides(layer,item['rendered_sample_count'],item.get('audio_sha256'))
  item['delivery_playback_speed']=1;item['delivery_gain_db']=0;item['delivery_fade_in_samples']=0;item['delivery_fade_out_samples']=0;layers.append(item)
  for field in ('section_effects','group_section_effects','group_section_effects_id','group_section_effects_applied','section_history','section_effects_applied','section_effect_added_sample_count'):
   if field in layer:item['processing_baked'][field]=copy.deepcopy(layer[field])
  if 'rendered_region' in layer:item['processing_baked']['rendered_region']=copy.deepcopy(layer['rendered_region'])
  if layer.get('rendered_envelopes'):item['processing_baked']['rendered_envelopes']=copy.deepcopy(layer['rendered_envelopes'])
  if 'processing_context' in layer:
   item['processing_baked']['processing_context']=copy.deepcopy(layer['processing_context'])
  if layer.get('source_mute_windows'):
   item['processing_baked']['source_mute_windows']=copy.deepcopy(layer['source_mute_windows']);item['processing_baked']['source_mute_clock_rate']=RATE
 track_streams=[]
 for bus in rendered.get('track_buses',[]):
  item={k:copy.deepcopy(v) for k,v in bus.items() if k!='waveform'}
  target=folder/('track__'+safe_component(bus['track_id'])+'.flac');encode_preview_pcm(bus_stems[bus['id']],target,channels=channels);timing=measure_delivery(target)
  if timing['audio_sample_count']!=bus['rendered_sample_count']:raise ValueError('A continuous track export changed its frame count.')
  item.update(**timing,audio_path=(relative/target.name).as_posix(),audio_sha256=file_hash(target),export_status='available',delivery_route='track_bus',kind='track_bus',rendered_sample_count=timing['audio_sample_count'])
  track_streams.append(item);decoded[item['id']]=decode(target,channels=channels)
 # Resolve dependencies on the measured delivery clock and check the editor's joins.
 scheduled=schedule_layers(layers);extent=max([*[layer['end_sample'] for layer in scheduled],*[bus['end_sample'] for bus in track_streams]])
 if any((layer['start_sample'],layer['end_sample'])!=(layer['editor_start_sample'],layer['editor_end_sample']) for layer in scheduled):
  raise ValueError('An exported clip changed the timing of the edited arrangement.')
 if extent!=authored_mix['audio_sample_count']:
  raise ValueError('The positioned files and the edited mix have different durations.')
 if not 0<extent<=RATE*1800:raise ValueError('Exported arrangement exceeds the preview duration limit.')
 mixed=array.array('f',[0])*(extent*channels)
 for layer in scheduled:
  for i,value in enumerate(decoded.get(layer['id'],())):mixed[layer['start_sample']*channels+i]+=value
 for bus in track_streams:
  for i,value in enumerate(decoded[bus['id']]):mixed[bus['start_sample']*channels+i]+=value
 preview=folder/'preview.flac';encode_preview_pcm(mixed,preview,channels=channels)
 words=[]
 for layer in scheduled:
  for word in rendered['words']:
   if word.get('layer_id')!=layer['id']:continue
   offset=layer['editor_start_sample']/RATE;length=layer['rendered_sample_count']/RATE;lo=max(0,min(length,word['start']-offset));hi=max(lo,min(length,word['end']-offset))
   if hi>lo:words.append({**word,'start':layer['start_sample']/RATE+lo,'end':layer['start_sample']/RATE+hi})
 payload={'format':'dmspeaks-rendered-arrangement','version':'0.1','arrangement_id':asset['id'],'dialogue_id':record['id'],'player':copy.deepcopy(asset.get('player',{})),'source_revision':record['revision'],'source_metadata_revision':record.get('metadata_revision'),'clock_sample_rate':RATE,'channels':channels,'sample_count':extent,'seconds':extent/RATE,'preview_path':(relative/'preview.flac').as_posix(),'processing':'Clip files already contain the editor tempo, gain, fades and mute settings. Play them at 1x/unity without reapplying effects. Preserve explicit overlap in the sequence.','timing_notice':'Decoded clip positions and duration match the edited arrangement. Target playback requires an adapter and runtime verification.','tracks':rendered['tracks'],'layers':scheduled,'words':words,'word_timing_source':'estimated_rescheduled_from_source_clip_timings; not realigned after encoding'}
 payload['authored_mix']=authored_mix
 payload['background_policy']='Music and ambience remain independent clips; they are never mixed into speech delivery clips. Original background bytes are included separately when present. The optional preview is a flattened listening cache.'
 if track_streams:
  payload.update(version='0.2',track_buses=track_streams,
   playback_streams=[*track_streams,*[dict(id=layer['id'],track_id=layer['track_id'],kind='clip',start_sample=layer['start_sample'],end_sample=layer['end_sample'],rendered_sample_count=layer['rendered_sample_count'],audio_path=layer['audio_path'],audio_sha256=layer['audio_sha256']) for layer in scheduled if layer.get('audio_path')]],
   processing='Play playback_streams once at their sample positions, at unity and normal speed. Continuous track stems already contain their member clips, effects, track controls and master gain. Member layers are editing/selection metadata, not additional audio streams. Changing a member selection requires rerendering its track bus.',
   required_capabilities=['positioned_parallel_streams','continuous_track_stems'])
 payload['replacement_selections']=[{'layer_id':layer['id'],'slot_order':layer['slot_order'],'placeholder':layer.get('slot',{}).get('placeholder'),'value':layer.get('slot',{}).get('value',''),'mode':'silence' if layer['kind']=='silence' else 'audio','audio_path':layer['audio_path'],'sample_count':layer['rendered_sample_count']} for layer in scheduled if layer.get('slot',{}).get('type') not in (None,'fixed') and not layer.get('clip_role')]
 if asset.get('visual_narration'):payload['visual_narration']=copy.deepcopy(asset['visual_narration'])
 if asset.get('track_hierarchy') is not None:payload['track_hierarchy']=copy.deepcopy(asset['track_hierarchy'])
 (folder/'arrangement.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
 from arrangement_csv import write_arrangement_csv
 write_arrangement_csv(folder/'arrangement.csv',scheduled,RATE,asset['id'],track_buses=track_streams)
 from background_library import choices_for_arrangement
 backgrounds=choices_for_arrangement(asset['id'],scheduled,rendered['tracks'],app.store)
 return {'arrangement_id':asset['id'],'dialogue_id':record['id'],'json_path':(relative/'arrangement.json').as_posix(),'csv_path':(relative/'arrangement.csv').as_posix(),'preview_path':payload['preview_path'],'authored_mix':authored_mix,'seconds':payload['seconds'],'clips':sum(bool(layer['audio_path']) for layer in scheduled),'track_stems':len(track_streams),'background_choices':backgrounds}
