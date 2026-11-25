"""Audition an exported sequence using packaged clips, with no provider calls."""
import copy,json,re,sqlite3,uuid
from contextlib import contextmanager
from pathlib import Path,PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import quote
from audio import encode_preview_pcm
from layered_audio import render_layers
from library_export import file_hash
from library_manifest import resolve_sequence,UnavailableSequence
from performance_plan import parse_entry
from timing_summary import summarize_timing

def _read_library_sequence(app,data,status):
 ident=data.get('export_id','')
 if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Choose a completed library export.')
 jobdir=app.folder/'exports'/ident;root=jobdir/'library';database=jobdir/'index.sqlite'
 if not database.is_file() or not (jobdir/'voxweave-audio-library.zip').is_file():raise ValueError('That library export is not available.')
 with sqlite3.connect('file:'+quote(str(database.resolve()))+'?mode=ro',uri=True) as index:
  row=index.execute('select value from annotated where record=?',(int(data['index']),)).fetchone()
 if not row:raise ValueError('This dialogue was not included in the library.')
 from library_delivery_checks import assert_library_delivery_usable
 assert_library_delivery_usable(app,jobdir)
 entry=json.loads(row[0]);player=data.get('player',{})
 if not all(key in entry for key in ['corpus_id','dialogue_id','dialogue_source_revision','source_index']):raise ValueError('Rebuild this older library export to test its clip sequence.')
 if not isinstance(player,dict):raise ValueError('Test character values must be an object.')
 pcm,arrangement,resolved,assets=render_exported_sequence(root,entry,player,data.get('variant','default'),data.get('reference_id') or None,data.get('generation_key') or None,data.get('silence_ms'),status)
 return entry,pcm,arrangement,resolved,assets


@contextmanager
def audition_library(app,data):
 """Serve one verified mix without adding recordings to the editing project."""
 with TemporaryDirectory(prefix='dmspeaks-library-audition-') as folder:
  path=Path(folder)/'audition.flac'
  with app.processing_work_lock:
   _,pcm,arrangement,resolved,_=_read_library_sequence(app,data,lambda _:None)
   encode_preview_pcm(pcm,path,channels=arrangement['channels'])
  skipped=sum(layer['kind']=='silence' and not layer['rendered_sample_count']
              for layer in resolved['layers'])
  headers={
   'X-DMSpeaks-Audition-Frames':str(arrangement['sample_count']),
   'X-DMSpeaks-Audition-Skipped':str(skipped),
   'X-DMSpeaks-Audition-Silent':str(len(resolved['missing_orders'])-skipped),
  }
  yield path,headers


def preview_library(app,data,status):
 entry,pcm,arrangement,resolved,assets=_read_library_sequence(app,data,status)
 ident=data['export_id'];player=data.get('player',{})
 path=app.audio.root/(uuid.uuid4().hex+'.flac');encode_preview_pcm(pcm,path,channels=arrangement.get('channels',1))
 # Own references to the exported copies: audition does not depend on originals remaining present.
 remap={}
 for original,source in assets.items():
  new=app.store.add_asset({**source,'id':uuid.uuid4().hex,'kind':'library-clip','source_clip_id':original,'dataset':entry['corpus_id'],'line_id':entry['dialogue_id']});remap[original]=new['id']
 for layer in arrangement['layers']:
  if layer.get('asset_id'):layer['asset_id']=remap[layer['asset_id']]
 for slot in arrangement['slots']:
  if slot.get('asset_id'):slot['asset_id']=remap[slot['asset_id']]
 asset=app.store.add_asset({'id':uuid.uuid4().hex,'kind':'library-preview','path':str(path),'dataset':entry['corpus_id'],'line_id':entry['dialogue_id'],'record_id':entry['corpus_id']+':'+str(entry['source_index']),'source_revision':entry['dialogue_source_revision'],'source_metadata_revision':entry.get('dialogue_metadata_revision'),'library_export_id':ident,'player':player,'sample_rate':44100,'channels':1,'missing_slots':resolved['missing_orders'],**summarize_timing(list(assets.values())),**arrangement})
 return {'asset':asset,'selection_context':resolved['selection_context'],'missing_orders':resolved['missing_orders'],'notice':'Audition uses packaged clips in the local mixer. Target-game playback is not verified.'}

def render_exported_sequence(root,entry,player,variant='default',reference_id=None,generation_key=None,silence_ms=None,status=lambda _:None,indexed_catalog=None):
 """Mix verified packaged clips without writing project assets or calling a provider."""
 root=Path(root)
 if entry.get('streamed_catalog'):
  from library_index import CatalogIndex
  owned=indexed_catalog is None
  catalog=indexed_catalog or CatalogIndex(root/'manifest.jsonl',root.parent/'runtime-index',lambda progress:status('Indexing exported catalog…'))
  try:entry={**entry,**catalog.entry(entry['streamed_catalog']['index'],player,variant,reference_id,generation_key)}
  finally:
   if owned:catalog.close()
 resolved=resolve_sequence(entry,player,variant,reference_id,generation_key,silence_ms)
 if resolved['sample_count']<=0:raise UnavailableSequence('This selection has no audible-duration clips or silence to preview.')
 source_text=entry.get('active_source_text',entry.get('source_text'))
 if not isinstance(source_text,str):raise ValueError('The exported dialogue has no source text for its placeholder sequence.')
 parsed={s['order']:s for s in parse_entry(entry,player)['segments']}
 assets={};layers=[];tracks={}
 for scheduled in resolved['layers']:
  layer={k:copy.deepcopy(v) for k,v in scheduled.items() if k not in {'clip','start_sample','end_sample','effective_overlap_samples'}}
  order=layer['slot_order'];segment=parsed[order];clip=scheduled['clip'];asset_id=None
  if clip:
   relative=PurePosixPath(clip['audio_path'])
   if relative.is_absolute() or '..' in relative.parts or '\\' in str(relative):raise ValueError('Invalid exported audio path.')
   path=(root/str(relative)).resolve()
   if not path.is_relative_to(root.resolve()) or not path.is_file():raise ValueError('A packaged clip is missing or outside its library.')
   if file_hash(path)!=clip['audio_sha256']:raise ValueError('A packaged clip has changed. Rebuild the library before previewing it.')
   sidecar=(root/clip['audio_metadata_path']).resolve()
   if not sidecar.is_relative_to(root.resolve()) or not sidecar.is_file():raise ValueError('A packaged clip has no valid timing sidecar.')
   source=json.loads(sidecar.read_text(encoding='utf-8'));asset_id=clip['clip_id']
   # Sidecars retain the master metadata. Restored assets describe the delivered
   # bytes and selection context, not nullable legacy fields or master lengths.
   assets[asset_id]={**source,'id':asset_id,'path':str(path),'sha256':clip['audio_sha256'],
                    'variant':clip['clip_variant'],'reference_id':clip.get('clip_reference_id',''),
                    'generation_key':clip.get('clip_generation_key',''),
                    'sample_count':clip['audio_sample_count'],'sample_rate':clip['audio_sample_rate'],
                    'channels':clip['audio_channels'],'seconds':clip['audio_seconds']}
  layer.update(asset_id=asset_id,original_sample_count=scheduled['rendered_sample_count'],source_in_sample=0,playback_speed=1,gain_db=0,muted=False,slot={**segment,'kind':layer['kind']})
  layers.append(layer);track_id=layer['track_id'];tracks.setdefault(track_id,{'id':track_id,'name':'Fixed speech' if track_id=='fixed' else segment['type'],'muted':False,'gain_db':0})
 class ExportedAssets:
  def asset(self,key):return assets[key]
 status('Mixing the exported clip sequence…');pcm,arrangement=render_layers(layers,list(tracks.values()),ExportedAssets())
 return pcm,arrangement,resolved,assets
