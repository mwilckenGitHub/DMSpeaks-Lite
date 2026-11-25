"""Audio assets, actual decoded waveforms and source-aware Ogg assembly."""
from __future__ import annotations
from engine_limits import MAX_SEGMENTS, MAX_AUDIO_SECONDS, silence_duration
import array,hashlib,io,json,math,os,re,shutil,struct,subprocess,uuid,wave
from functools import lru_cache
from pcm_input import pcm_input_file
from content_review import is_rejected,is_delivery_eligible,assert_audio_usable
from model import delimiter_source_end,segment_audio_identity,segment_audio_matches
from pathlib import Path

RATES={16000,22050,24000,32000,44100,48000,96000}
VORBIS_PAGE_OPTIONS = ('-page_duration', '1')

def run(args,timeout=180):
 r=subprocess.run(args,capture_output=True,timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
 if r.returncode:raise ValueError('Audio processing failed: '+r.stderr.decode(errors='replace')[-700:])
 return r.stdout

def probe(path,*,expected_format=None):
 data=json.loads(run(['ffprobe','-v','error','-show_entries','format=duration,format_name:stream=codec_type,sample_rate,channels,codec_name','-of','json',str(path)],30))
 streams=data.get('streams',[])
 if expected_format and (data.get('format',{}).get('format_name')!=expected_format or
                          [stream.get('codec_type') for stream in streams]!=['audio']):
  raise ValueError('Choose a matching audio file with one audio stream.')
 stream=next((s for s in streams if s.get('codec_type')=='audio'),None)
 if not stream:raise ValueError('The file has no decodable audio stream.')
 seconds=float(data['format'].get('duration') or 0)
 if not 0<seconds<=MAX_AUDIO_SECONDS:raise ValueError('Use an audio clip between zero and 30 minutes.')
 return {'seconds':seconds,'sample_rate':int(stream['sample_rate']),'channels':int(stream['channels']),'codec':stream.get('codec_name')}

def decode(path,sr=44100,channels=1):
 from vorbis_pcm import is_vorbis,read_vorbis
 if is_vorbis(path):
  pcm,source_rate,source_channels=read_vorbis(path)
  if source_rate==sr and source_channels==channels:return pcm
  with pcm_input_file(pcm) as source:
   data=run(['ffmpeg','-v','error','-nostdin','-f','f32le','-ar',str(source_rate),'-ac',str(source_channels),'-i',str(source),'-f','f32le','-ac',str(channels),'-ar',str(sr),'-'],180)
 else:data=run(['ffmpeg','-v','error','-nostdin','-i',str(path),'-vn','-f','f32le','-ac',str(channels),'-ar',str(sr),'-'],180)
 pcm=array.array('f');pcm.frombytes(data);return pcm

def encode_pcm(pcm,path,sr=44100,channels=1):
 """Encode exactly the supplied frames; Vorbis granules retain the true end."""
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 if type(channels) is not int or channels not in (1,2) or not pcm or len(pcm)%channels:raise ValueError('Use nonempty mono or stereo PCM.')
 with pcm_input_file(pcm) as source:
  run(['ffmpeg','-v','error','-nostdin','-y','-f','f32le','-ar',str(sr),'-ac',str(channels),'-i',str(source),'-c:a','libvorbis','-q:a','5',*VORBIS_PAGE_OPTIONS,str(path)])
 from vorbis_pcm import measure_vorbis
 actual,rate,count=measure_vorbis(path)
 if actual!=len(pcm)//channels or rate!=sr or count!=channels:raise ValueError('Ogg export did not preserve the supplied audio frame count.')
 return path

def encode_preview_pcm(pcm,path,sr=44100,channels=1):
 """Lossless preview cache keeps the shared sample clock exact; export chooses delivery codec."""
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 with pcm_input_file(pcm) as source:
  run(['ffmpeg','-v','error','-nostdin','-y','-f','f32le','-ar',str(sr),'-ac',str(channels),'-i',str(source),'-c:a','flac','-sample_fmt','s32',str(path)])
 return path

def short_playback_wav(asset):
 """Offer seekable browser playback for short FLACs without replacing the asset."""
 path=Path(asset['path'])
 frames,rate,channels=(asset.get(key) for key in ('sample_count','sample_rate','channels'))
 if asset.get('kind')=='recording-source':
  # The layer references the 44.1 kHz editing clock. Playback reads the
  # original file, which may have a different rate and sample count.
  source=asset.get('source_audio_metadata') or {}
  rate,seconds,channels=(source.get(key) for key in ('sample_rate','seconds','channels'))
  frames=round(seconds*rate) if rate in RATES and isinstance(seconds,(int,float)) and 0<seconds<=1 else None
 if rate is None and isinstance(asset.get('layers'),list):rate=44100
 if (path.suffix.lower()!='.flac' or type(frames) is not int or rate not in RATES or
     channels not in (1,2) or not 0<frames<=rate):return None
 return _playback_wav(path,asset.get('id'),frames,rate,channels)

@lru_cache(maxsize=32)
def _playback_wav(path,asset_id,frames,rate,channels):
 # Chrome can fail to seek short FLAC streams. Decode to integer PCM without
 # resampling, remixing, or changing the samples used for export and analysis.
 pcm=run(['ffmpeg','-v','error','-nostdin','-i',str(path),'-map','0:a:0','-t','1.1',
          '-c:a','pcm_s32le','-f','s32le','-'],30)
 if len(pcm)!=frames*channels*4:raise ValueError('Preview audio does not match its saved sample count.')
 return _pcm_wav(pcm,rate,channels)

def playback_span_wav(asset,start,end):
 """Read an exact span from a saved recording on the editor clock."""
 frames,rate,channels=(asset.get(key) for key in ('sample_count','sample_rate','channels'))
 if rate is None and isinstance(asset.get('layers'),list):rate=44100
 if rate!=44100 or channels not in (1,2) or type(frames) is not int or not 0<=start<end<=frames:
  raise ValueError('Choose a valid sample span from this recording.')
 path=asset['path']
 from vorbis_pcm import is_vorbis,read_vorbis
 if is_vorbis(path):
  import numpy as np
  decoded,source_rate,source_channels=read_vorbis(path,span=(start,end))
  if (source_rate,source_channels)!=(rate,channels):
   decoded=decode(path,sr=rate,channels=channels)[start*channels:end*channels]
  # Match the WAV PCM representation used for other recordings, including
  # saturation if Vorbis reconstruction overshoots full scale.
  integer_samples=np.asarray(decoded,dtype=np.float64)
  integer_samples*=2**31
  np.rint(integer_samples,out=integer_samples)
  np.clip(integer_samples,-2**31,2**31-1,out=integer_samples)
  pcm=integer_samples.astype('<i4').tobytes()
 else:
  pcm=run(['ffmpeg','-v','error','-nostdin','-i',str(path),'-map','0:a:0',
           '-af',f'aresample={rate},atrim=start_sample={start}:end_sample={end}',
           '-ac',str(channels),'-c:a','pcm_s32le','-f','s32le','-'],30)
 if len(pcm)!=(end-start)*channels*4:raise ValueError('The saved recording does not match its clip timing.')
 return _pcm_wav(pcm,rate,channels)

def _pcm_wav(pcm,rate,channels):
 output=io.BytesIO()
 with wave.open(output,'wb') as stream:
  stream.setparams((channels,4,rate,0,'NONE','not compressed'))
  stream.writeframes(pcm)
 return output.getvalue()

def fade(pcm,sr=44100):
 a=array.array('f',pcm);n=min(round(.003*sr),len(a)//4)
 for i in range(n):
  factor=math.sin(i/(max(1,n-1))*math.pi/2)**2;a[i]*=factor;a[-1-i]*=factor
 if a:a[0]=a[-1]=0
 return a

def nearest_zero(pcm,at,sr=44100):
 center=round(at*sr);lo=max(1,center-round(.002*sr));hi=min(len(pcm)-2,center+round(.002*sr))
 values=[i for i in range(lo,hi) if pcm[i]*pcm[i+1]<=0]
 return min(values,key=lambda i:abs(i-center))+1 if values else min(max(1,center),len(pcm)-1)

class Audio:
 def __init__(self,root,store):self.root=Path(root);self.store=store;self.root.mkdir(parents=True,exist_ok=True);self.peak_cache={}
 def register(self,path,*,preserve_imported_vorbis=False,lossless=False,**meta):
  path=Path(path);info=probe(path);ident=uuid.uuid4().hex
  channels=info['channels']
  if channels not in (1,2):raise ValueError('Use a mono or stereo audio source.')
  preserve=False
  if preserve_imported_vorbis and info['codec']=='vorbis':
   from vorbis_pcm import is_vorbis, measure_vorbis, single_stream_end
   if is_vorbis(path):
    end=single_stream_end(path)
    preserve=info['sample_rate']==44100
    if preserve:
     try:
      actual_count,rate,measured_channels=measure_vorbis(path)
     except RuntimeError as error:
      raise ValueError('The Ogg Vorbis audio could not be decoded.') from error
     if (rate,measured_channels)!=(44100,channels) or not 0<actual_count<=MAX_AUDIO_SECONDS*44100 or end!=actual_count:
      raise ValueError('The Ogg Vorbis file has inconsistent audio timing.')
  target=self.root/(ident+('.flac' if lossless and not preserve else '.ogg'))
  try:
   if preserve:
    shutil.copyfile(path,target)
    actual=info
   else:
    encode=encode_preview_pcm if lossless else encode_pcm
    encode(decode(path,channels=channels),target,channels=channels)
    actual=probe(target);actual_count=len(decode(target,channels=channels))//channels
   if preserve_imported_vorbis:
    meta['source_bytes_preserved']=preserve
   return self.store.add_asset({**meta,'id':ident,'path':str(target),'seconds':actual_count/44100,'sample_count':actual_count,'container_seconds':actual['seconds'],'sample_rate':44100,'channels':channels,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'words':meta.get('words',[]),'timing_source':meta.get('timing_source','unavailable')})
  except Exception:
   target.unlink(missing_ok=True)
   raise
 def silence(self,ms):
  ms=silence_duration(ms)
  # Zero-duration placeholders occupy no samples; browser/assembly tracks them without an empty Ogg.
  if ms==0:return None
  for a in self.store.assets():
   if a.get('kind')=='silence' and a.get('silence_ms')==ms:return a
  p=self.root/('silence-'+str(ms)+'.ogg');encode_pcm(array.array('f',[0])*round(ms*44.1),p)
  return self.store.add_asset({'id':'silence-'+str(ms),'path':str(p),'seconds':ms/1000,'sample_rate':44100,'channels':1,'kind':'silence','silence_ms':ms,'words':[],'timing_source':'silence'})
 def peaks(self,asset_id,bins=1200):
  bins=max(100,min(32000,int(bins)));asset=self.store.asset(asset_id)
  from phoneme_alignment import with_timeline_words
  asset=with_timeline_words(self.store,asset)
  regions=asset.get('slots') or asset.get('clip_regions') or []
  cuts=tuple(sorted({round(float(r[k])*44100) for r in regions for k in ('start','end') if k in r}))
  key=(asset_id,bins,cuts)
  if key not in self.peak_cache:
   channels=probe(asset['path'])['channels'];pcm=decode(asset['path'],channels=channels);n=len(pcm)//channels;step=max(1,n//bins)
   edges=sorted({0,n,*range(0,n,step),*(max(0,min(n,c)) for c in cuts)})
   starts=edges[:-1];ends=edges[1:];peaks=[max((abs(v) for v in pcm[a*channels:b*channels]),default=0) for a,b in zip(starts,ends)]
   if len(self.peak_cache)>=64:self.peak_cache.pop(next(iter(self.peak_cache)))
   self.peak_cache[key]={'peaks':peaks,'seconds':n/44100,'sample_rate':44100,'sample_count':n,'step_samples':step,'peak_start_samples':starts,'peak_end_samples':ends}
  return {**self.peak_cache[key],'words':asset.get('words',[]),'timing_source':asset.get('timing_source','unavailable'),'alignment':asset.get('alignment')}
 def export(self,asset_id,fmt='ogg',sample_rate=44100,channels=1,quality=4,bitrate=128,depth=24):
  from replacement_playback import delivery_asset
  asset=delivery_asset(self.store,self.store.asset(asset_id))
  if asset.get('empty_arrangement'):raise ValueError('The arrangement is empty. Add audio clips before exporting.')
  if asset.get('kind')=='comparison-preview':raise ValueError('Comparison audio is for audition only. Export the normal selected dialogue clips.')
  assert_audio_usable(asset,self.store)
  return self._encode_export(asset,fmt,sample_rate,channels,quality,bitrate,depth)
 def _encode_export(self,asset,fmt='ogg',sample_rate=44100,channels=1,quality=4,bitrate=128,depth=24):
  """Encode a caller-validated asset to a new file; never change its source."""
  if fmt not in {'ogg','wav','mp3','flac'}:raise ValueError('Choose Ogg, WAV, MP3, or FLAC.')
  if int(sample_rate) not in RATES or int(channels) not in (1,2):raise ValueError('Unsupported sample rate or channel count.')
  if int(bitrate) not in (32,48,64,80,96,112,128,160,192,224,256,320):raise ValueError('Choose a standard MP3 bitrate.')
  if fmt=='mp3':
   if int(sample_rate)>48000:raise ValueError('MP3 export supports sample rates up to 48,000 Hz.')
   if int(sample_rate)<32000 and int(bitrate)>160:
    raise ValueError('MP3 export below 32,000 Hz supports bitrates up to 160 kb/s.')
  if fmt=='flac' and int(depth) not in (16,24):raise ValueError('This FLAC encoder supports 16 or 24-bit export.')
  if fmt=='wav' and int(depth) not in (16,24,32):raise ValueError('Choose 16, 24 or 32-bit WAV export.')
  target=self.root/(uuid.uuid4().hex+'.'+fmt)
  args=['ffmpeg','-v','error','-nostdin','-y','-i',asset['path'],'-ar',str(sample_rate),'-ac',str(channels)]
  if fmt=='ogg':args+=['-c:a','libvorbis','-q:a',str(max(0,min(10,int(quality)))),*VORBIS_PAGE_OPTIONS]
  elif fmt=='mp3':args+=['-c:a','libmp3lame','-b:a',str(bitrate)+'k']
  elif fmt=='flac':args+=['-c:a','flac','-sample_fmt','s16' if int(depth)==16 else 's32']
  else:args+=['-c:a',{16:'pcm_s16le',24:'pcm_s24le',32:'pcm_f32le'}[int(depth)]]
  try:
   from vorbis_pcm import is_vorbis
   if is_vorbis(asset['path']):
    # Decode with granule-aware boundaries before format/rate conversion.
    info=probe(asset['path']);pcm=decode(asset['path'],info['sample_rate'],info['channels'])
    with pcm_input_file(pcm) as source:
     input_args=['ffmpeg','-v','error','-nostdin','-y','-f','f32le','-ar',str(info['sample_rate']),'-ac',str(info['channels']),'-i',str(source)]
     run(input_args+args[7:]+[str(target)])
   else:run(args+[str(target)])
   probe(target);return target
  except Exception:
   target.unlink(missing_ok=True)
   raise
 def candidate_query(self,record,segment,variant='default',reference_id='',generation_key='',**kwargs):
  from authoring_catalog import CandidateQuery
  return CandidateQuery(self.store,record,segment,variant,reference_id,generation_key,**kwargs)
 def compatible(self,record,segments,variant='default',reference_id='',generation_key='',assets=None,*,summary=False,limit=50,selections=None):
  result=[]
  for s in segments:
   if s.get('speech_excluded'):
    result.append({**s,'options':[],'review_candidates':[],'options_total':0,'review_total':0});continue
   if assets is None:
    query=self.candidate_query(record,s,variant,reference_id,generation_key)
    page=query.page(limit=limit,full=not summary,selected=(selections or {}).get(str(s['order'])))
    pending=query.page(limit=limit,pending=True)
    result.append({**s,'options':[{k:v for k,v in a.items() if k!='path'} for a in page['candidates']],
     'review_candidates':pending['candidates'],'options_total':page['total'],'review_total':pending['total']});continue
   from clip_ownership import belongs_to_record
   candidates=[a for a in assets if belongs_to_record(a,record) and a.get('kind')=='piece' and not is_rejected(a) and (not generation_key or a.get('generation_key')==generation_key) and (not a.get('dataset') or a['dataset']==record['dataset']) and a.get('line_id')==record['id'] and str(a.get('npc_id') or '')==str(record['metadata'].get('npc_id') or '') and a.get('slot_order')==s['order'] and a.get('voice')==record['metadata'].get('npc_voice_profile') and a.get('variant','default')==variant and (not reference_id or a.get('reference_id','')==reference_id) and (s['type']=='fixed' or a.get('value')==s['value']) and a.get('source_revision')==record['revision'] and (a.get('source_metadata_revision')==record.get('metadata_revision') if a.get('source_metadata_revision') is not None else not record.get('metadata_changed',False))]
   candidates=[a for a in candidates if segment_audio_matches(a,s)]
   options=[a for a in candidates if is_delivery_eligible(a)]
   pending=[{k:a.get(k) for k in ['id','value','seconds']} for a in candidates if not is_delivery_eligible(a)]
   result.append({**s,'options':[{k:v for k,v in a.items() if k!='path'} for a in options],'review_candidates':pending,'options_total':len(options),'review_total':len(pending)})
  return result
 def assemble(self,record,segments,selections,player,silence_ms=200,variant='default',reference_id='',generation_key='',*,snap_mode=None):
  silence_ms=silence_duration(silence_ms)
  if len(segments)>MAX_SEGMENTS:raise ValueError(f'This assembly exceeds the current runtime capacity of {MAX_SEGMENTS} segments.')
  compatible=self.compatible(record,segments,variant,reference_id,generation_key,summary=True,limit=1,selections=selections);pcm=array.array('f');slots=[];words=[];missing=[];timing_assets=[]
  for s in compatible:
   options=s['options'];requested=selections.get(str(s['order']));chosen_id=None if requested=='silence' else requested or (options[0]['id'] if options else None)
   if requested=='silence' and s['type']=='fixed' and s['spoken_text']:raise ValueError('Fixed speech cannot be replaced by a missing-variable placeholder.')
   if chosen_id and not any(a['id']==chosen_id for a in options):raise ValueError('The selected clip does not match this speaker, text revision, slot and test value.')
   start=len(pcm)/44100
   if chosen_id:
    asset=self.store.asset(chosen_id);chunk=decode(asset['path']);kind='audio';timing_assets.append(asset)
   elif s.get('speech_excluded'):chunk=array.array('f');kind='direction';asset=None
   elif s['type']!='fixed':
    chunk=array.array('f',[0])*round(int(silence_ms)*44.1);kind='silence';asset=self.silence(silence_ms);missing.append(s['order'])
   elif not s['spoken_text']:chunk=array.array('f');kind='direction';asset=None
   else:raise ValueError(f'Fixed segment {s["order"]} has no audio yet. Generate this passage first or import its compatible clip.')
   pcm.extend(chunk);end=len(pcm)/44100
   item={k:v for k,v in s.items() if k!='options'};item.update(start=start,end=end,duration=end-start,start_sample=round(start*44100),end_sample=len(pcm),sample_count=len(chunk),original_clip_sample_count=len(chunk),original_clip_seconds=len(chunk)/44100,original_clip_sample_rate=44100,playback_speed=1.0,record_id=record['dataset']+':'+str(record['index']),asset_id=asset['id'] if asset else None,kind=kind)
   slots.append(item)
   if asset:
    for w in asset.get('words',[]):words.append({**w,'start':start+w['start'],'end':start+w['end'],'source_start':s['source_start'] if s['type']!='fixed' else w.get('source_start',s['source_start']),'source_end':delimiter_source_end(s) if s['type']!='fixed' else w.get('source_end',s['source_end']),'slot_order':s['order']})
  if not pcm:raise ValueError('No audio is available to assemble.')
  from layered_audio import default_layers,render_layers
  layers,tracks=default_layers(slots)
  from delimiter_definitions import registered
  from replacement_snap import select_for_delimiter
  definitions={item['key']:item for item in registered(record)}
  snap_settings={'snap_mode':snap_mode} if snap_mode is not None else {}
  for layer in layers:
   if layer['kind']=='audio' and layer['slot']['type']!='fixed':
    select_for_delimiter(layer,record,snap_settings,definitions=definitions)
   if layer.get('asset_id') and self.store.asset(layer['asset_id']).get('preserve_whole_take'):layer.update(fade_in_samples=0,fade_out_samples=0)
  pcm,arrangement=render_layers(layers,tracks,self.store)
  slots=arrangement['slots'];words=arrangement['words']
  path=self.root/(uuid.uuid4().hex+'.flac');encode_preview_pcm(pcm,path,channels=arrangement.get('channels',1))
  from timing_summary import summarize_timing
  timing=summarize_timing(timing_assets)
  asset=self.store.add_asset({'id':uuid.uuid4().hex,'path':str(path),'seconds':len(pcm)/44100,'sample_rate':44100,'channels':1,'kind':'assembly','dataset':record['dataset'],'line_id':record['id'],'record_id':record['dataset']+':'+str(record['index']),'source_revision':record['revision'],'source_metadata_revision':record.get('metadata_revision'),'player':player,'slots':slots,'words':words,**timing,'missing_slots':missing,**arrangement})
  return asset
 def split(self,record,segments,full_asset,aligned,variant='default',reference_id='',generation_key='',only_orders=None,*,lossless=False):
  assert_audio_usable(full_asset,self.store)
  channels=probe(full_asset['path'])['channels']
  if channels not in (1,2):raise ValueError('Use a mono or stereo source take.')
  pcm=decode(full_asset['path'],channels=channels);total=len(pcm)//channels
  signal=pcm if channels==1 else pcm[::2] if sum(abs(v) for v in pcm[::2])>=sum(abs(v) for v in pcm[1::2]) else pcm[1::2]
  bounds=[0];cursor=0
  def tokens(s):return re.findall(r"[^\W_]+(?:['’][^\W_]+)*",s.lower(),re.UNICODE)
  wordlist=[]
  for index,w in enumerate(aligned):
   for token in tokens(w['word']):wordlist.append(dict(w,token=token,timing_word_index=index))
  target=[t for s in segments for t in tokens(s['spoken_text'])]
  if len(target)!=len(wordlist):raise ValueError('Word alignment does not match the resolved source; keeping full take without unsafe splits.')
  for s in segments[:-1]:
   cursor+=len(tokens(s['spoken_text']))
   if cursor==0:cut=bounds[-1]
   elif cursor>=len(wordlist):cut=total
   else:cut=nearest_zero(signal,(wordlist[cursor-1]['end']+wordlist[cursor]['start'])/2)
   if cut<bounds[-1]:raise ValueError('The estimated clip boundaries are not ordered.')
   bounds.append(cut)
  bounds.append(total);assets=[];cursor=0
  alignment_evidence={}
  if lossless:
   from alignment_backend import source_alignment_fingerprint
   alignment_evidence['source_alignment_sha256']=source_alignment_fingerprint(full_asset)
  full_asset['clip_regions']=[{**{k:v for k,v in s.items() if k!='options'},'start':bounds[i]/44100,'end':bounds[i+1]/44100,'duration':(bounds[i+1]-bounds[i])/44100} for i,s in enumerate(segments)]
  for i,s in enumerate(segments):
   begin,end=bounds[i:i+2];count=len(tokens(s['spoken_text']))
   if (only_orders is not None and s['order'] not in only_orders) or end<=begin or not count:cursor+=count;continue
   local=[]
   for w in wordlist[cursor:cursor+count]:local.append({'word':w['word'],'start':max(0,w['start']-begin/44100),'end':min((end-begin)/44100,w['end']-begin/44100),'source_start':w.get('source_start',s['source_start']),'source_end':w.get('source_end',s['source_end'])})
   selected_indices=list(dict.fromkeys(w['timing_word_index'] for w in wordlist[cursor:cursor+count]))
   cursor+=count;path=self.root/(uuid.uuid4().hex+'.flac');chunk=array.array('f',pcm[begin*channels:end*channels])
   if not lossless:
    # Fade each channel on the same frame clock, never alternate channel samples.
    for channel in range(channels):chunk[channel::channels]=fade(chunk[channel::channels])
   encode_preview_pcm(chunk,path,channels=channels)
   delivery_count=len(decode(path,channels=channels))//channels
   from timing_evidence import for_piece
   digest=hashlib.sha256(path.read_bytes()).hexdigest()
   evidence=for_piece(full_asset,selected_indices,begin/44100,digest,working_words=aligned)
   piece_evidence={'sha256':digest,**alignment_evidence}
   boundary_method=('estimated word boundary, zero crossing within2ms, no fades' if lossless
                    else 'estimated word boundary, zero crossing within2ms, 3ms edge fades')
   if evidence is not None:piece_evidence['word_timing_evidence']=evidence
   assets.append(self.store.add_asset({**piece_evidence,'id':uuid.uuid4().hex,'path':str(path),'kind':'piece','dataset':record['dataset'],'record_id':record['dataset']+':'+str(record['index']),'generation_key':generation_key,'line_id':record['id'],'npc_id':record['metadata'].get('npc_id'),'voice':record['metadata'].get('npc_voice_profile'),'variant':variant,'reference_id':reference_id,'source_revision':record['revision'],'source_metadata_revision':record.get('metadata_revision'),'slot_order':s['order'],'slot_type':s['type'],'value':s['value'],**segment_audio_identity(s),'insert_expected_text':s['spoken_text'],'isolation_review_required':lossless or bool(s.get('segment_model') or s.get('performance_model')),'seconds':delivery_count/44100,'sample_count':delivery_count,'sample_rate':44100,'channels':channels,'words':local,'timing_source':full_asset.get('timing_source','estimated'),'alignment':full_asset.get('alignment'),'source_take_asset_id':full_asset['id'],'original_sample_count':end-begin,'original_sample_rate':44100,'original_seconds':(end-begin)/44100,'source_start_sample':begin,'source_end_sample':end,'boundary_method':boundary_method,'listening_review':'pending'}))
  return assets
