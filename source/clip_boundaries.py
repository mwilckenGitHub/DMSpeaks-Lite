"""Audition full source takes and rebuild immutable pieces without shrinking speech."""
import array,copy,hashlib,math,uuid
from pathlib import Path
from shutil import copyfile
from audio import probe,decode,encode_pcm,encode_preview_pcm
from content_review import apply_review,requires_isolation_review
RATE=44100
CONTEXT_FRAMES=round(.15*RATE)

def outward_edges(pcm,begin,end,window=88):
 if any(type(value) is not int for value in [begin,end,window]) or window<0 or not 0<=begin<end<=len(pcm):raise ValueError('Choose an ordered source sample range within the take.')
 start=begin;stop=end
 for index in range(begin,max(0,begin-window)-1,-1):
  if index==0 or pcm[index-1]*pcm[index]<=0:start=index;break
 for index in range(end,min(len(pcm),end+window)+1):
  if index==len(pcm) or pcm[index-1]*pcm[index]<=0:stop=index;break
 return start,stop

def digest(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as source:
  for block in iter(lambda:source.read(1024*1024),b''):h.update(block)
 return h.hexdigest()

def resolve_source(store,ident):
 selected=store.asset(ident);piece=selected;seen=set()
 while piece.get('kind')=='library-clip' and piece.get('source_clip_id'):
  if piece['id'] in seen:raise ValueError('Source clip lineage contains a cycle.')
  seen.add(piece['id']);piece=store.asset(piece['source_clip_id'])
 if piece.get('native_join_slice'):
  receipt=piece['native_join_slice']
  if not isinstance(receipt,dict) or not isinstance(receipt.get('return_sha256'),str):
   raise ValueError('This replacement has no saved source range.')
  original=store.asset(piece.get('source_clip_id',''))
  identity=('record_id','line_id','source_revision','source_metadata_revision','slot_order','value')
  if (original.get('kind')!='piece' or not original.get('native_context_window') or
      original.get('sha256')!=receipt.get('return_sha256') or
      any(original.get(key)!=piece.get(key) for key in identity) or
      not Path(original['path']).is_file() or digest(original['path'])!=receipt['return_sha256']):
   raise ValueError('The original Fish return is unavailable or belongs to a different clip.')
  return selected,piece,original
 if piece.get('kind')!='piece':raise ValueError('Choose a speech clip, not a silent slot or full mix.')
 retained_id=piece.get('boundary_source_asset_id')
 if retained_id:
  retained=store.asset(retained_id)
  identity=('record_id','dataset','line_id','source_revision','slot_order','slot_type','value')
  if (retained.get('kind')!='piece' or retained['id']==piece['id'] or
      retained.get('boundary_source_asset_id') or
      retained.get('source_metadata_revision') not in (None,piece.get('source_metadata_revision')) or
      any(retained.get(key)!=piece.get(key) for key in identity)):
   raise ValueError('The retained recording belongs to a different clip or dialogue.')
  if not Path(retained['path']).is_file() or digest(retained['path'])!=piece.get('boundary_source_sha256'):
   raise ValueError('The original retained clip audio is missing or changed.')
  return selected,piece,retained
 if (requires_isolation_review(piece) and piece.get('source_start_sample') is None and
     piece.get('source_end_sample') is None):
  # A native replacement's conditioning take may pronounce a different word.
  # The isolated return is the only editable recording in this case.
  if not Path(piece['path']).is_file() or (piece.get('sha256') and digest(piece['path'])!=piece['sha256']):
   raise ValueError('The retained clip audio is missing or changed.')
  return selected,piece,piece
 parent_id=piece.get('source_take_asset_id')
 if not parent_id:
  with store.lock:rows=store.db.execute('select metadata from takes order by rowid desc').fetchall()
  import json
  for row in rows:
   take=json.loads(row[0])
   if piece['id'] in take.get('piece_ids',[]):parent_id=take['asset_id'];break
 if not parent_id:raise ValueError('The complete source take is unavailable. Import a complete take or render a new one.')
 parent=store.asset(parent_id)
 if parent.get('kind')!='take' or not Path(parent['path']).is_file():raise ValueError('The complete source take is unavailable.')
 return selected,piece,parent

def source_pcm(path):
 channels=probe(path)['channels']
 if channels not in (1,2):raise ValueError('Boundary review requires a mono or stereo source take.')
 return decode(path,channels=channels),channels

def source_waveform(pcm, channels, begin, end):
 """Measure a bounded detail view on the original recording's frame clock."""
 start=max(0,begin-CONTEXT_FRAMES)
 stop=min(len(pcm)//channels,end+CONTEXT_FRAMES)
 stride=max(1,math.ceil((stop-start)/600))
 peaks=[max(abs(value) for value in pcm[left*channels:min(stop,left+stride)*channels])
        for left in range(start,stop,stride)]
 return {'start_sample':start,'end_sample':stop,'step_samples':stride,'peaks':peaks}

def source_info(app,ident):
 selected,piece,parent=resolve_source(app.store,ident)
 pcm,channels=source_pcm(parent['path'])
 count=len(pcm)//channels
 begin,end=editable_range(piece,parent,count)
 return {'piece_id':selected['id'],'source_asset_id':parent['id'],'source_sha256':digest(parent['path']),'sample_rate':RATE,'sample_count':count,'channels':channels,'start_sample':begin,'end_sample':end,'timing_source':parent.get('timing_source','unavailable'),'words':parent.get('words',[]),'waveform':source_waveform(pcm,channels,begin,end),'join_audition_supported':True,'retained_clip_only':parent['kind']=='piece','isolation_review_required':requires_isolation_review(piece) or requires_isolation_review(selected),'native_join':bool(piece.get('native_join_slice'))}

def editable_range(piece,parent,count):
 if piece.get('native_join_slice'):
  samples=piece['native_join_slice'].get('return_samples')
  if not isinstance(samples,list) or len(samples)!=2:
   raise ValueError('This replacement has no saved source range.')
  begin,end=samples
 elif parent['id']==piece['id']:
  begin,end=0,count
 elif parent['kind']=='piece':
  begin,end=piece.get('boundary_source_start_sample'),piece.get('boundary_source_end_sample')
 else:
  begin,end=piece.get('source_start_sample'),piece.get('source_end_sample')
 if type(begin) is not int or type(end) is not int or not 0<=begin<end<=count:
  raise ValueError('This clip has no valid source sample mapping.')
 return begin,end

def current_record(app,data,piece):
 from clip_ownership import belongs_to_record,current_record as resolve_record
 record=resolve_record(app.store,data['dataset'],int(data['index']))
 if not belongs_to_record(piece,record) or piece.get('dataset') not in (None,'',record['dataset']) or piece.get('line_id')!=record['id'] or piece.get('source_revision')!=record['revision']:raise ValueError('This clip belongs to another dialogue or source revision.')
 if data.get('source_revision')!=record['revision'] or data.get('source_metadata_revision')!=record['metadata_revision']:raise ValueError('The source changed. Reopen boundary review.')
 if (piece.get('source_metadata_revision')!=record['metadata_revision'] if piece.get('source_metadata_revision') is not None else record.get('metadata_changed',False)):raise ValueError('This clip uses earlier metadata. Re-render it before boundary review.')
 return record

def check_source_owner(record,parent):
 from clip_ownership import belongs_to_record
 if (not belongs_to_record(parent,record) or parent.get('dataset') not in (None,'',record['dataset']) or
     parent.get('line_id') not in (None,'',record['id'])):
  raise ValueError('This source take belongs to another dialogue or has ambiguous ownership.')

def rebind_timing_evidence(piece, source_sha256, begin, output_sha256):
 """Keep the original returned times on their source clock after a recut."""
 from timing_evidence import returned_groups
 old_sample=piece.get('source_start_sample')
 if type(old_sample) is not int:return None
 old_start=old_sample/RATE
 observations={
  channel:copy.deepcopy(observation)
  for channel,_,offset,observation in returned_groups(piece)
  if observation.get('audio_sha256')==source_sha256 and
     math.isclose(offset,old_start,rel_tol=0,abs_tol=.5/RATE)
 }
 if not observations:return None
 try:
  if digest(piece['path'])!=piece.get('sha256'):return None
 except OSError:return None
 return {**copy.deepcopy(piece['word_timing_evidence']),'observations':observations,
         'source_offset_seconds':begin/RATE,'bound_audio_sha256':output_sha256}

def audition(app,data,status):
 selected,piece,parent=resolve_source(app.store,data['piece_id']);record=current_record(app,data,piece)
 check_source_owner(record,parent)
 if data.get('source_sha256')!=digest(parent['path']):raise ValueError('The source audio changed. Reopen boundary review.')
 if type(data.get('outward_handles',True)) is not bool:raise ValueError('Outward handles must be true or false.')
 pcm,channels=source_pcm(parent['path']);signal=pcm if channels==1 else pcm[::2] if sum(abs(v) for v in pcm[::2])>=sum(abs(v) for v in pcm[1::2]) else pcm[1::2]
 editable_range(piece,parent,len(pcm)//channels)
 begin=data.get('start_sample');end=data.get('end_sample')
 match=data.get('match_source_pauses',False)
 if type(match) is not bool:raise ValueError('Choose whether to match source pauses.')
 pause_match={}
 if match:
  from native_pause_match import for_boundary
  if channels!=1 or data.get('outward_handles',True):
   raise ValueError('Pause matching needs an exact mono replacement range.')
  outward_edges(signal,begin,end,0)
  (begin,end),proof=for_boundary(app,record,data,piece,parent,pcm,(begin,end))
  pause_match={'pause_match':proof}
 start,stop=outward_edges(signal,begin,end,88 if data.get('outward_handles',True) else 0)
 chunk=array.array('f',pcm[start*channels:stop*channels]);lead=begin-start;tail=stop-end
 # Any envelope is confined to extra handles, never the user-selected speech core.
 for i in range(lead):
  for c in range(channels):chunk[i*channels+c]*=math.sin(math.pi/2*i/max(1,lead))**2
 for i in range(tail):
  for c in range(channels):chunk[(len(chunk)//channels-tail+i)*channels+c]*=math.cos(math.pi/2*(i+1)/max(1,tail))**2
 warnings=[]
 expected={word.get('word','').casefold() for word in piece.get('words',[])}
 for word in parent.get('words',[]):
  if word['start']<end/RATE and word['end']>begin/RATE and word.get('word','').casefold() not in expected:warnings.append('Selection reaches estimated neighboring word '+word.get('word','')+'. Check by listening; timestamps are approximate.')
 status('Building the full selected source interval…');path=app.audio.root/(uuid.uuid4().hex+'.flac');encode_preview_pcm(chunk,path,channels=channels)
 try:
  current_record(app,data,piece)
  return app.store.add_asset({'id':uuid.uuid4().hex,'kind':'boundary-preview','audition_only':True,'path':str(path),'sha256':digest(path),'record_id':record['dataset']+':'+str(record['index']),'source_revision':record['revision'],'source_metadata_revision':record['metadata_revision'],'boundary_piece_id':selected['id'],'boundary_source_asset_id':parent['id'],'source_sha256':data['source_sha256'],'selection_start_sample':begin,'selection_end_sample':end,'source_start_sample':start,'source_end_sample':stop,'speech_core_start_sample':lead,'speech_core_end_sample':lead+end-begin,'sample_rate':RATE,'channels':channels,'sample_count':len(chunk)//channels,'seconds':len(chunk)/channels/RATE,'boundary_warnings':warnings,'words':[],'timing_source':'manual interval; word alignment not verified',**pause_match})
 except Exception:path.unlink(missing_ok=True);raise

def join_excerpt(pcm,channels,layer,path,base_id,audition_id):
 first=max(0,layer['start_sample']-CONTEXT_FRAMES)
 last=min(len(pcm)//channels,layer['end_sample']+CONTEXT_FRAMES)
 encode_preview_pcm(pcm[first*channels:last*channels],path,channels=channels)
 return {'kind':'boundary-join-preview','path':str(path),'sha256':digest(path),
         'audition_only':True,'base_asset_id':base_id,'audition_id':audition_id,
         'sample_rate':RATE,'sample_count':last-first,'seconds':(last-first)/RATE,
         'channels':channels,'timeline_start_sample':first,'timeline_end_sample':last}

def recut_native_join(base, target, piece, new, parent, begin, end):
 """Keep the original return clock and rebase estimated labels on a manual cut."""
 original_start=piece['native_join_slice']['return_samples'][0]
 timing=target.pop('native_join_timing',{})
 words=[]
 partial=False
 for word in timing.get('words',[]):
  start,stop=(original_start+round(word[key]*RATE) for key in ('start','end'))
  lo,hi=max(begin,start),min(end,stop)
  partial|=(lo,hi)!=(start,stop)
  if lo<hi:words.append({**word,'start':(lo-begin)/RATE,'end':(hi-begin)/RATE})
 new['native_join_slice']={'return_sha256':parent['sha256'],'return_samples':[begin,end]}
 if timing.get('audio_sha256')==piece.get('sha256'):
  target['native_join_timing']={'audio_sha256':new['sha256'],'words':words,
                                'timing_source':('partial_' if partial else '')+'estimated_manual_join_words'}
 else:words=[]
 proof={key:value for key,value in base['join_proposal'].items() if key!='pause_match'}
 return {**proof,'timing_basis':'manual_boundary','insert_region':[begin,end],
         'replacement_words':words,'excluded_return_samples':parent['sample_count']-(end-begin)}


def rebuild(app,data,status):
 join_preview=data.get('preview_join',False)
 if type(join_preview) is not bool:raise ValueError('Choose whether to audition the proposed join.')
 selected,piece,parent=resolve_source(app.store,data['piece_id']);record=current_record(app,data,piece);preview=app.store.asset(data['audition_id']);base=app.store.asset(data['base_asset_id'])
 from processing_definition import require_ordinary_rebuild
 from content_review import is_rejected
 scoped=base.get('kind')=='scoped-arrangement-preview' and base.get('audition_only') is True
 native=bool(piece.get('native_join_slice'))
 if not scoped:require_ordinary_rebuild(base,allow_track_hierarchy=native)
 if is_rejected(base):raise ValueError('This recording was rejected. Reassemble with replacement clips.')
 if base.get('replacement_playback'):
  raise ValueError('Return to the selected dialogue mix before reviewing its clip boundaries.')
 check_source_owner(record,parent)
 preview_source=preview.get('boundary_source_asset_id') or preview.get('source_take_asset_id')
 if preview.get('kind')!='boundary-preview' or preview.get('boundary_piece_id')!=selected['id'] or preview_source!=parent['id'] or preview.get('source_sha256')!=digest(parent['path']):raise ValueError('Build a fresh audition for this source clip before saving.')
 if preview.get('source_revision')!=record['revision'] or preview.get('source_metadata_revision')!=record['metadata_revision']:raise ValueError('The source changed after the audition.')
 if base.get('record_id')!=record['dataset']+':'+str(record['index']) or base.get('source_revision')!=record['revision'] or base.get('source_metadata_revision')!=record['metadata_revision']:raise ValueError('Reassemble the current source before replacing its clip boundary.')
 base_layers=base.get('layers',[])
 target_index=next((index for index,layer in enumerate(base_layers)
                    if layer['id']==data['layer_id'] and layer.get('asset_id')==selected['id']),None)
 if target_index is None:raise ValueError('The selected layer changed. Reopen boundary review.')
 if native and (not base.get('join_proposal') or base_layers[target_index]['id']!='proposed-replacement'):
  raise ValueError('Open this replacement in its saved dialogue mix to adjust the boundary.')
 pause_match=preview.get('pause_match')
 if pause_match and (not native or pause_match.get('base_asset_id')!=base['id']):
  raise ValueError('The dialogue mix changed. Match the source pauses again.')
 isolation_required=requires_isolation_review(piece) or requires_isolation_review(selected)
 audition_sha=preview.get('sha256')
 if audition_sha and digest(preview['path'])!=audition_sha:
  raise ValueError('The audition audio changed. Build and listen to a fresh audition.')
 if isolation_required and not native and not join_preview and (
     data.get('confirm_isolation') is not True or not audition_sha or data.get('isolation_review_sha256')!=audition_sha):
  raise ValueError('Listen to a fresh isolated audition and confirm its pronunciation before saving.')
 layers=copy.deepcopy(base_layers);target=layers[target_index]
 path=app.audio.root/(uuid.uuid4().hex+('.flac' if isolation_required else '.ogg'))
 mixpath=currentpath=drypath=None
 try:
  pcm,channels=source_pcm(preview['path'])
  if isolation_required:
   copyfile(preview['path'],path)
   actual=len(pcm)//channels
  else:
   encode_pcm(pcm,path,channels=channels)
   actual=len(decode(path,channels=channels))//channels
  new={k:copy.deepcopy(v) for k,v in piece.items() if k not in {
   'id','path','sha256','word_timing_evidence','content_review','content_review_revision',
   'content_review_history','delivery_derivation','clip_processing'}}
  begin=preview['source_start_sample'];end=preview['source_end_sample']
  new.update(id=uuid.uuid4().hex,path=str(path),channels=channels,original_sample_count=len(pcm)//channels,original_sample_rate=RATE,original_seconds=len(pcm)/channels/RATE,sample_count=actual,seconds=actual/RATE,source_metadata_revision=record['metadata_revision'],boundary_parent_piece_id=piece['id'],speech_core_start_sample=preview['speech_core_start_sample'],speech_core_end_sample=preview['speech_core_end_sample'],boundary_method='auditioned source range; outward handles only; selected speech core unchanged',listening_review='user chose and saved source interval',timing_source='estimated_manual_boundary_source_words',words=[])
  if native:
   new['listening_review']='pending'
  elif parent['kind']=='piece':
   new.update(boundary_source_asset_id=parent['id'],boundary_source_sha256=preview['source_sha256'],
              boundary_source_start_sample=begin,boundary_source_end_sample=end)
  else:
   new.update(source_take_asset_id=parent['id'],source_start_sample=begin,source_end_sample=end)
  new['sha256']=digest(path)
  if isolation_required:
   new['isolation_review_required']=True
   if audition_sha and new['sha256']!=audition_sha:
    raise ValueError('The audition audio changed. Build and listen to a fresh audition.')
   if not join_preview and not native:
    apply_review(new,{'status':'user_verified','listened':True,'expected_revision':0,
                      'media_sha256':audition_sha,'isolated_content':True,
                      'complete_pronunciation':True},lambda:new['sha256'])
  evidence=rebind_timing_evidence(piece,preview['source_sha256'],begin,new['sha256'])
  if evidence is not None:new['word_timing_evidence']=evidence
  for word in parent.get('words',[]):
   lo=max(begin/RATE,word['start']);hi=min(end/RATE,word['end'])
   if hi>lo:new['words'].append({**word,'start':lo-begin/RATE,'end':hi-begin/RATE})
  # Keep only this slot's transcript words; timing itself is still estimated.
  if piece.get('words'):
   expected=[w.get('word','').casefold() for w in piece['words']];new['words']=[w for w in new['words'] if w.get('word','').casefold() in expected]
  if native and target.get('replacement_snap'):
   # A recut creates a new asset for the same recording. Keep its authored
   # settings while leaving settings for every other recording attached to it.
   from replacement_snap import references, remap
   remap(target,{ident:new['id'] if ident==selected['id'] else ident for ident in references(target)})
  target.update(asset_id=new['id'],source_in_sample=0,source_out_sample=actual,original_sample_count=actual,original_sample_rate=RATE,original_seconds=actual/RATE)
  native_proposal=recut_native_join(base,target,piece,new,parent,begin,end) if native else None
  if pause_match:
   native_proposal['pause_match']={key:value for key,value in pause_match.items() if key!='base_asset_id'}
  if not native:target.update(fade_in_samples=0,fade_out_samples=0)
  from processing_group_lock import validate_transition,validate_scheduled
  hierarchy=base.get('track_hierarchy')
  if scoped and hierarchy is not None:
   from audio_track_hierarchy import TrackHierarchy
   model=TrackHierarchy.from_mapping(hierarchy,base['processing_definition'],tracks=base['tracks'],layers=layers)
   locks=model.effective_locks()
   if any(track.get('group_locked',False)!=locks[track['id']] for track in base['tracks']):
    raise ValueError('The saved track locks do not match their folders.')
  validate_transition(base['tracks'],base['layers'],base['tracks'],layers)
  from layered_audio import render_layers
  status('Building the proposed join…' if join_preview else 'Rebuilding the layers with the complete selected clip…')
  class PendingPiece:
   def asset(self,ident):return new if ident==new['id'] else app.store.asset(ident)
  inspection=base.get('audition_only') is True and base.get('kind') in {'inspection-preview','scoped-arrangement-preview'}
  mixed,arrangement=render_layers(layers,base['tracks'],PendingPiece(),audition_only=join_preview or inspection or native)
  validate_scheduled(base['tracks'],base['layers'],arrangement['layers'])
  mix_channels=arrangement.get('channels',1)
  metadata={k:copy.deepcopy(v) for k,v in base.items()
            if k not in {'id','path','content_review','content_review_revision','content_review_history'}}
  metadata.update(arrangement,id=uuid.uuid4().hex,parent_assembly_id=base['id'],
                  boundary_repair_piece_id=new['id'])
  if native:
   metadata.update(kind='inspection-preview',audition_only=True,join_proposal=native_proposal,
                   listening_review='pending')
   metadata.pop('contextual_delivery',None)
  dry=None
  if scoped:
   # Reapply the authored stack on the new dry clock in the bounded worker.
   # Neither the provisional clip nor the dry graph is visible before Save.
   dry={k:v for k,v in metadata.items()
        if k not in {'processing_definition','processing_preview','dry_sample_count','track_hierarchy'}}
   dry['kind']='inspection-preview'
   if not join_preview:
    drypath=app.audio.root/(uuid.uuid4().hex+'.flac')
    encode_preview_pcm(mixed,drypath,channels=mix_channels)
    dry.update(path=str(drypath),sha256=digest(drypath))
   mixed=None
   metadata=app._processing_arrangement({'asset_id':dry['id'],'stack':base['processing_definition']},
       status,persist=False,transient_base=dry,pending_piece=new)
   mixpath=Path(metadata['path'])
   if hierarchy is not None:metadata['track_hierarchy']=copy.deepcopy(hierarchy)
  if join_preview:
   if scoped:
    mixed,mix_channels=source_pcm(mixpath)
    mixpath.unlink()
   mixpath=app.audio.root/(uuid.uuid4().hex+'.flac')
   scheduled=next(layer for layer in arrangement['layers'] if layer['id']==target['id'])
   proposed=join_excerpt(mixed,mix_channels,scheduled,mixpath,base['id'],preview['id'])
   currentpath=app.audio.root/(uuid.uuid4().hex+'.flac')
   current_pcm,current_channels=source_pcm(base['path'])
   current=join_excerpt(current_pcm,current_channels,base_layers[target_index],currentpath,base['id'],preview['id'])
   current_record(app,data,piece)
   path.unlink(missing_ok=True)
   app.store.add_assets([current,proposed])
   return {**proposed,'current_join_id':current['id']}
  if not scoped:
   mixpath=app.audio.root/(uuid.uuid4().hex+'.flac')
   encode_preview_pcm(mixed,mixpath,channels=mix_channels)
   metadata.update(path=str(mixpath),sha256=digest(mixpath))
  current_record(app,data,piece)
  app.store.add_assets([new,dry,metadata] if scoped else [new,metadata])
  return {'piece':new,'asset':metadata,'slot_order':target['slot_order']}
 except Exception:
  path.unlink(missing_ok=True)
  if mixpath is not None:mixpath.unlink(missing_ok=True)
  if currentpath is not None:currentpath.unlink(missing_ok=True)
  if drypath is not None:drypath.unlink(missing_ok=True)
  raise
