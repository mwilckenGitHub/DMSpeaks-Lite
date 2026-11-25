"""Non-destructive speech layers, sequenced across tracks on a shared sample clock."""
from engine_limits import MAX_SEGMENTS, MAX_AUDIO_SECONDS
import array,copy,math
from collections import OrderedDict
from pathlib import Path
from pcm_input import pcm_input_file
from audio import decode,run,probe
from clip_snap_points import clip_snap_points, word_belongs_to_clip
from model import delimiter_source_end
from stereo_frames import frame_count,trim_frames,pan_frame,convert_channels
RATE=44100


class LayerSourceCache:
    """Reuse decoded source PCM for one render or comparison.

    Clip trimming, gain, effects and word timing are evaluated for every use.
    Waveform activity may be reused when processed frames and thresholds match.
    """

    def __init__(self, max_bytes=24 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.bytes = 0
        self.entries = OrderedDict()
        self.activity_points = {}

    @staticmethod
    def _identity(path):
        try:
            stat = path.stat()
        except OSError as error:
            raise ValueError('A source recording is unavailable for audio processing.') from error
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def read(self, filename):
        path = Path(filename)
        identity = self._identity(path)
        prior = self.entries.pop(path, None)
        if prior is not None:
            saved_identity, pcm, channels, size = prior
            if saved_identity == identity:
                self.entries[path] = prior
                return pcm, channels
            self.bytes -= size
        channels = probe(filename)['channels']
        if channels not in (1, 2):
            raise ValueError('Use a mono or stereo source clip.')
        pcm = decode(filename, channels=channels)
        if self._identity(path) != identity:
            raise ValueError('A source recording changed during audio processing. Try again.')
        size = len(pcm) * pcm.itemsize
        if 0 < size <= self.max_bytes:
            while self.bytes + size > self.max_bytes:
                _, (_, _, _, removed_size) = self.entries.popitem(last=False)
                self.bytes -= removed_size
            self.entries[path] = identity, pcm, channels, size
            self.bytes += size
        return pcm, channels

def _context_word_spans(layer,asset):
 """Use the matched neighbor spans only with the donor timing they describe."""
 links=layer.get('context_word_links')
 if not isinstance(links,dict) or links.get('audio_sha256')!=asset.get('sha256'):
  return {}
 spans=links.get('spans')
 if not isinstance(spans,dict):
  return {}
 from alignment_backend import source_alignment_fingerprint
 return spans if links.get('alignment_sha256')==source_alignment_fingerprint(asset) else {}


def _context_join_word_labels(words, layers):
 """Keep one label when the same fixed word touches both sides of a context cut."""
 from guide_bindings import parent_id

 by_id={layer['id']:layer for layer in layers}
 visible=[]
 for word in words:
  if visible:
   prior=visible[-1]
   left,right=by_id[prior['layer_id']],by_id[word['layer_id']]
   same_word=all(prior.get(field)==word.get(field) for field in ('word','source_start','source_end'))
   linked_context=any(
    core['id'].startswith('source-core:') and packet['id'].startswith('packet:')
    for core,packet in ((left,right),(right,left)))
   if (same_word and word['source_end']>word['source_start'] and linked_context
       and left.get('slot',{}).get('type')==right.get('slot',{}).get('type')=='fixed'
       and parent_id(right)==left['id']
       and round(prior['end']*RATE)==left['end_sample']==right['start_sample']==round(word['start']*RATE)):
    if word['end']-word['start']>prior['end']-prior['start']:
     visible[-1]=word
    continue
  visible.append(word)
 return visible


def stretch(pcm,speed,channels=1):
 if speed==1 or not pcm:return array.array('f',pcm)
 filters=[];remaining=speed
 while remaining>2:filters.append('atempo=2');remaining/=2
 filters.append('atempo='+str(remaining))
 with pcm_input_file(pcm) as source:
  raw=run(['ffmpeg','-v','error','-nostdin','-f','f32le','-ar',str(RATE),'-ac',str(channels),'-i',str(source),'-af',','.join(filters),'-f','f32le','-acodec','pcm_f32le','-'])
 result=array.array('f');result.frombytes(raw)
 if pcm and not result:raise ValueError('This clip is too short for the requested tempo change.')
 return result

def clip_envelope(pcm,channels,fade_in,fade_out,gain_db):
 result=array.array('f',pcm);count=frame_count(result,channels);fade_in=min(fade_in,count);fade_out=min(fade_out,count);gain=10**(gain_db/20)
 for i in range(count):
  envelope=gain
  if fade_in>1 and i<fade_in:envelope*=math.sin(math.pi/2*i/(fade_in-1))**2
  if fade_out>1 and count-1-i<fade_out:envelope*=math.sin(math.pi/2*(count-1-i)/(fade_out-1))**2
  for c in range(channels):result[i*channels+c]*=envelope
 return result

def default_layers(slots):
 layers=[];previous=None;tracks={}
 for slot in slots:
  if slot['kind']=='direction':continue
  kind=slot['type']
  track='fixed' if kind=='fixed' else 'delimiter:'+kind
  name='Fixed speech' if kind=='fixed' else kind[:1].upper()+kind[1:]
  tracks.setdefault(track,{'id':track,'name':name,'muted':False,'gain_db':0})
  ident='slot-'+str(slot['order']);length=slot['end_sample']-slot['start_sample']
  # Sequencing must preserve the recorded onset and decay. In particular,
  # consecutive sections of one performance must reconstruct that performance
  # without a dip at every join. Fades are explicit, non-destructive edits.
  layers.append({'id':ident,'track_id':track,'slot_order':slot['order'],'asset_id':slot.get('asset_id'),'kind':slot['kind'],'after_layer_id':previous,'anchor_start_sample':slot['start_sample'],'original_sample_count':length,'original_sample_rate':RATE,'original_seconds':length/RATE,'source_in_sample':0,'source_out_sample':length,'playback_speed':1.0,'fade_in_samples':0,'fade_out_samples':0,'gain_db':0.0,'gap_samples':0,'overlap_samples':0,'muted':False,'slot':copy.deepcopy(slot)})
  previous=ident
 return layers,list(tracks.values())

def integer(value,label,lower=0,upper=RATE*MAX_AUDIO_SECONDS):
 if not isinstance(value,int) or isinstance(value,bool) or not lower<=value<=upper:raise ValueError('Invalid layer '+label+'.')
 return value

def render_layers(layers,tracks,store,stems=None,*,audition_only=False,bus_stems=None,speech_regions=None,normalize_master=True,analysis_sink=None,source_cache=None):
 from layer_schedule import schedule_layers
 if not isinstance(layers,list) or not 0<=len(layers)<=MAX_SEGMENTS:raise ValueError(f'A speech arrangement allows up to {MAX_SEGMENTS} clips for this runtime.')
 if not isinstance(tracks,list) or any(not isinstance(t,dict) or not isinstance(t.get('id'),str) for t in tracks):raise ValueError('Invalid audio tracks.')
 trackmap={t['id']:t for t in tracks}
 if len(trackmap)!=len(tracks):raise ValueError('Track identifiers must be unique.')
 from effects_schema import validate_effects
 from track_buses import validate_track
 from background_duck import validate_ducking,held_regions,iter_duck_gains,apply_duck
 if type(normalize_master) is not bool:raise ValueError('Master normalization must be true or false.')
 if analysis_sink is not None and not callable(analysis_sink):raise ValueError('Invalid read-only analysis sink.')
 ducking={}
 for track in tracks:
  if 'output_route' in track:
   from output_routing import validate_assignment
   validate_assignment(track['output_route'])
  validate_effects(track.get('effects'))
  validate_track(track)
  ducking[track['id']]=validate_ducking(track.get('ducking'))
  if ducking[track['id']]['enabled'] and track.get('role') not in {'music','ambience'}:raise ValueError('Speech ducking belongs to a music or ambience track.')
  if 'effects_bypassed' in track and not isinstance(track['effects_bypassed'],bool):raise ValueError('Track effect bypass must be true or false.')
  if track.get('channel_mode','auto') not in {'auto','mono','stereo'}:raise ValueError('Choose source, mono or stereo track channels.')
  pan=track.get('pan',0)
  if isinstance(pan,bool) or not isinstance(pan,(int,float)) or not math.isfinite(pan) or not -1<=pan<=1:raise ValueError('Track pan must be between -1 and +1.')
  for key in ['muted','solo']:
   if key in track and not isinstance(track[key],bool):raise ValueError('Track '+key+' must be true or false.')
 stereo=any(track.get('pan',0)!=0 or track.get('channel_mode')=='stereo' for track in tracks);any_solo=any(track.get('solo',False) for track in tracks)
 if source_cache is None:source_cache=LayerSourceCache()
 prepared=[];chunks={};source_assets={};timing_scaled=False
 for raw in layers:
  layer=copy.deepcopy(raw)
  if not isinstance(layer.get('id'),str) or not layer['id'] or layer.get('track_id') not in trackmap:raise ValueError('Each layer needs a unique ID and an existing track.')
  speed=layer.get('playback_speed',1);gain=layer.get('gain_db',0)
  if isinstance(speed,bool) or not isinstance(speed,(int,float)) or not math.isfinite(speed) or not .75<=speed<=2.5:raise ValueError('Layer speed must be 0.75–2.5×.')
  if isinstance(gain,bool) or not isinstance(gain,(int,float)) or not math.isfinite(gain) or not -60<=gain<=12:raise ValueError('Layer gain must be -60 to +12 dB.')
  for key in ['anchor_start_sample','gap_samples','overlap_samples','fade_in_samples','fade_out_samples']:layer[key]=integer(layer.get(key,0),key)
  source_channels=1
  if layer.get('kind')=='direction':source=array.array('f')
  elif layer.get('kind')=='silence':source=array.array('f',[0])*integer(layer.get('original_sample_count',0),'silence length',0,RATE*MAX_AUDIO_SECONDS)
  else:
   asset=store.asset(layer['asset_id'])
   if layer.get('clip_role') in {'background_music','background_ambience'}:
    import hashlib
    if asset.get('kind')!='background-source' or hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest()!=asset.get('sha256'):raise ValueError('The background source changed. Import the current file again.')
   from content_review import assert_audio_usable
   if layer.get('clip_role')=='visual_narration':
    from content_review import assert_narration_usable
    assert_narration_usable(asset)
   assert_audio_usable(asset,store,audition_only=audition_only)
   source,source_channels=source_cache.read(asset['path'])
   if 'native_join_timing' in layer:
    from native_join_evidence import timeline_word_asset
    asset=timeline_word_asset(layer,asset)
   else:
    from phoneme_alignment import with_timeline_words
    asset=with_timeline_words(store,asset)
   source_assets[layer['id']]=asset
  media_count=frame_count(source,source_channels)
  original_source_channels=source_channels
  channel_mode=trackmap[layer['track_id']].get('channel_mode','auto')
  target_channels={'mono':1,'stereo':2}.get(channel_mode,source_channels)
  if target_channels!=source_channels:source=convert_channels(source,source_channels,target_channels);source_channels=target_channels
  stereo|=source_channels==2
  if 'source_mute_windows' in layer:
   if layer.get('kind')!='audio':raise ValueError('Source mute windows require an audio layer.')
   if layer.get('source_mute_clock_rate',RATE)!=RATE:raise ValueError('Source mute windows use the editor source clock at 44100 Hz.')
   from source_mutes import apply_source_mutes
   source=apply_source_mutes(source,source_channels,layer['source_mute_windows']);layer['source_mute_clock_rate']=RATE
  loop_info=None
  if layer.get('background_loop') is not None:
   if layer.get('clip_role') not in {'background_music','background_ambience'}:raise ValueError('Loop policies require a music or ambience clip.')
   from background_loop import loop_source
   source,loop_info=loop_source(source,source_channels,layer['background_loop'],speed,stretch)
  layer['source_media_sample_count']=media_count
  if layer.get('clip_role')=='imported_audio':
   # Desktop attachments retain their native clock. Use the decoded editor
   # clock for the initial source range and all subsequent trim operations.
   layer.setdefault('source_out_sample',media_count)
  if loop_info:layer['background_loop_info']=loop_info
  else:layer.pop('background_loop_info',None)
  begin=integer(layer.get('source_in_sample',0),'source in',0,frame_count(source,source_channels));end=integer(layer.get('source_out_sample',frame_count(source,source_channels)),'source out',begin,frame_count(source,source_channels))
  original=frame_count(source,source_channels);timing_scaled|=speed!=1 and layer.get('clip_role') not in {'background_music','background_ambience','imported_audio'}
  from section_processing import process
  processing_track=trackmap[layer['track_id']]
  # A missing replacement has no signal for the track's delay/reverb to decay.
  # Keep the authored rack for the next recording, without manufacturing a tail
  # on a timed silence (or turning a zero-duration skip into a pause).
  if layer.get('kind') in {'silence','direction'}:
   processing_track={**processing_track,'effects_bypassed':True}
  processing_layer={**layer,'playback_speed':1} if loop_info else layer
  chunk,effect_info,count=process(source,source_channels,processing_layer,processing_track,begin,end,stretch,clip_envelope,integer)
  layer.update(effect_info)
  total_count=frame_count(chunk,source_channels)
  timing_count=effect_info.get('word_source_dry_sample_count',effect_info.get('pre_region_dry_sample_count',count))
  layer.update(original_sample_count=original,original_sample_rate=RATE,original_seconds=original/RATE,source_channels=source_channels,original_source_channels=original_source_channels,trimmed_source_sample_count=end-begin,rendered_sample_count=total_count,rendered_seconds=total_count/RATE,effective_speed=(end-begin)/timing_count if timing_count else 1)
  layer['snap_points']=clip_snap_points(chunk,source_channels,RATE,layer,source_assets.get(layer['id']),
                                       activity_cache=source_cache.activity_points)
  prepared.append(layer);chunks[layer['id']]=chunk
 scheduled=schedule_layers(prepared);extent=max((layer['end_sample'] for layer in scheduled),default=1)
 if not 0<extent<=RATE*MAX_AUDIO_SECONDS:raise ValueError('A mixed preview must be greater than zero and at most 30 minutes.')
 channels=2 if stereo else 1
 from track_buses import render as render_buses
 buses,bus_outputs=render_buses(scheduled,tracks,chunks,channels)
 extent=max([extent,*[bus['end_sample'] for bus in buses]])
 if speech_regions is None:
  speech_regions=[]
  for layer in scheduled:
   track=trackmap[layer['track_id']]
   if layer['kind']=='audio' and layer.get('clip_role') not in {'background_music','background_ambience','imported_audio'} and track.get('role') not in {'music','ambience'} and not (layer.get('muted') or track.get('muted') or any_solo and not track.get('solo')):
    speech_regions.append((layer['start_sample'],layer['end_sample']))
  for bus in buses:
   if trackmap[bus['track_id']].get('role') not in {'music','ambience'} and not bus['muted']:speech_regions.append((bus['start_sample'],bus['end_sample']))
 speech_regions=held_regions(speech_regions,RATE,0)
 for bus in buses:
  policy=ducking[bus['track_id']]
  if policy['enabled']:
   from track_buses import waveform
   signal=apply_duck(bus_outputs[bus['id']],channels,bus['start_sample'],speech_regions,RATE,policy)
   bus_outputs[bus['id']]=signal;bus.update(ducking=copy.deepcopy(policy),ducking_applied=True,waveform=waveform(signal,channels))
 from join_edge_review import mark_join_activity,mark_join_edges,speech_clip_audible
 mixed=array.array('f',[0])*(extent*channels);slots=[];words=[];audible_speech=set()
 for layer in scheduled:
  chunk=chunks[layer['id']];track=trackmap[layer['track_id']];track_gain=track.get('gain_db',0)
  if isinstance(track_gain,bool) or not isinstance(track_gain,(int,float)) or not math.isfinite(track_gain) or not -60<=track_gain<=12:raise ValueError('Invalid track gain.')
  gain=10**(track_gain/20);muted=bool(layer.get('muted') or track.get('muted') or any_solo and not track.get('solo'));pan=track.get('pan',0);source_channels=layer['source_channels'];left,right=pan_frame(1,1,pan,source_channels);count=frame_count(chunk,source_channels)
  start=layer['start_sample'];layer['dry_end_sample']=start+layer['dry_rendered_sample_count']
  routed=bool(layer.get('track_bus_id'))
  policy=ducking[track['id']]
  duck=iter_duck_gains(speech_regions,start,count,RATE,policy) if policy['enabled'] and not routed else None
  if policy['enabled']:layer.update(ducking=copy.deepcopy(policy),ducking_applied=not routed)
  else:layer.pop('ducking',None);layer.pop('ducking_applied',None)
  stem=array.array('f',chunk) if stems is not None and routed else array.array('f') if stems is not None else None
  if analysis_sink is not None:analysis_sink(layer,chunk,source_channels,gain,dict(speech_regions=speech_regions,policy=policy) if policy['enabled'] and not routed else None)
  step=max(1,math.ceil(count/600));peaks=[0.0]*math.ceil(count/step);channel_peaks=[[0.0]*len(peaks) for _ in range(source_channels)]
  if chunk:
   for i in range(count):
    value=chunk[i*source_channels];rsource=chunk[i*source_channels+1] if source_channels==2 else value
    attenuation=next(duck) if duck is not None else 1
    value=value*gain*attenuation;rsource=rsource*gain*attenuation;peaks[i//step]=max(peaks[i//step],abs(value),abs(rsource));channel_peaks[0][i//step]=max(channel_peaks[0][i//step],abs(value))
    if source_channels==2:channel_peaks[1][i//step]=max(channel_peaks[1][i//step],abs(rsource))
    if stereo:
     lvalue=0 if muted else value*left;rvalue=0 if muted else rsource*right
     if stem is not None and not routed:stem.extend((lvalue,rvalue))
     if not routed:mixed[(start+i)*2]+=lvalue;mixed[(start+i)*2+1]+=rvalue
    else:
     if stem is not None and not routed:stem.append(0 if muted else value)
     if not muted and not routed:mixed[start+i]+=value
  if stems is not None:stems[layer['id']]=stem
  layer['waveform']={'peaks':peaks,'channel_peaks':channel_peaks,'channels':source_channels,'sample_count':count,'step_samples':step,'sample_rate':RATE,'stage':'per-clip envelope, effects and track gain before master level'}
  # Background audio has its own lane and extent, never a text-replacement slot
  # or spoken-word alignment. Its effects remain local to that lane.
  if layer.get('clip_role') in {'background_music','background_ambience','imported_audio'}:continue
  if speech_clip_audible(layer,track,any_solo):
   audible_speech.add(layer['id'])
  slot=copy.deepcopy(layer.get('slot',{}));slot.update(order=layer['slot_order'],asset_id=layer.get('asset_id'),kind=layer['kind'],start=start/RATE,end=layer['end_sample']/RATE,duration=layer['rendered_sample_count']/RATE,start_sample=start,end_sample=layer['end_sample'],original_clip_sample_count=layer['original_sample_count'],original_clip_sample_rate=RATE,original_clip_seconds=layer['original_seconds'],playback_speed=layer.get('playback_speed',1));slots.append(slot)
  asset=source_assets.get(layer['id']);effective=layer['effective_speed'];trim=layer.get('source_in_sample',0)/RATE
  if asset:
   offset=layer.get('word_offset_samples',layer.get('rendered_region',{}).get('start_sample',0))/RATE
   duration=layer['dry_rendered_sample_count']/RATE
   context_spans=_context_word_spans(layer,asset) if slot.get('type')=='fixed' else {}
   for index,word in enumerate(asset.get('words') or []):
    if not word_belongs_to_clip(layer,word):continue
    lo=max(0,(word['start']-trim)/effective-offset);hi=min(duration,(word['end']-trim)/effective-offset)
    # A word touching a clip edge owns no samples on that side of the cut.
    if round(hi*RATE)<=round(lo*RATE):continue
    span=context_spans.get(str(index))
    if isinstance(span,list) and len(span)==2 and all(type(value) is int for value in span) and 0<=span[0]<span[1]:
     word={**word,'source_start':span[0],'source_end':span[1]}
    positioned={**word,'layer_id':layer['id'],'start':start/RATE+lo,'end':start/RATE+hi,'source_start':slot.get('source_start',0) if slot.get('type')!='fixed' else word.get('source_start',slot.get('source_start',0)),'source_end':delimiter_source_end(slot) if slot.get('type')!='fixed' else word.get('source_end',slot.get('source_end',0)),'slot_order':layer['slot_order']}
    if 'phonemes' in word:
     from phoneme_alignment import position_phones
     phones=position_phones(word['phonemes'],start=lo,end=hi,trim=trim,speed=effective,offset=offset)
     positioned['phonemes']=[{**phone,'start':start/RATE+phone['start'],'end':start/RATE+phone['end']} for phone in phones]
    word_begin,word_end=round(word['start']*RATE),round(word['end']*RATE)
    masks=layer.get('source_mute_windows',[])
    if any(mask['silence_start']<=word_begin and word_end<=mask['silence_end'] for mask in masks):positioned['source_mute_state']='silenced_before_effects'
    elif any(mask['fade_down_start']<word_end and word_begin<mask['fade_up_end'] for mask in masks):positioned['source_mute_state']='partially_attenuated_before_effects'
    if layer.get('clip_role')=='visual_narration':positioned.update(narration_source_start=word.get('source_start'),narration_source_end=word.get('source_end'),source_start=slot.get('source_start',0),source_end=slot.get('source_end',0),speaker_role='narrator')
    words.append(positioned)
 for bus in buses:
  start=bus['start_sample']*channels
  for i,value in enumerate(bus_outputs[bus['id']]):mixed[start+i]+=value
 peak=max((abs(value) for value in mixed),default=0);master_gain=min(1,.98/peak) if peak and normalize_master else 1
 if master_gain<1:mixed=array.array('f',(value*master_gain for value in mixed))
 mark_join_edges(mixed,channels,scheduled,audible_speech)
 mark_join_activity(scheduled,chunks,trackmap,audible_speech)
 if stems is not None and master_gain<1:
  routed_ids={layer['id'] for layer in scheduled if layer.get('track_bus_id')}
  for ident,stem in stems.items():
   if ident not in routed_ids:stems[ident]=array.array('f',(value*master_gain for value in stem))
 if bus_stems is not None:
  for ident,signal in bus_outputs.items():bus_stems[ident]=array.array('f',(value*master_gain for value in signal)) if master_gain<1 else signal
 words.sort(key=lambda w:(w['start'],w['end']))
 timing={}
 has_packets=any(layer['id'].startswith('packet:') for layer in scheduled)
 if words and (has_packets or any('native_join_timing' in layer for layer in scheduled)):
  from timing_summary import summarize_timing
  if has_packets:words=_context_join_word_labels(words,scheduled)
  speech_assets=[source_assets[layer['id']] for layer in scheduled
                 if layer['id'] in audible_speech and layer['id'] in source_assets]
  timing=summarize_timing(speech_assets)
 return mixed,{'empty_arrangement':not scheduled,'layers':scheduled,'tracks':copy.deepcopy(tracks),'track_buses':buses,'slots':slots,'words':words,'sample_count':extent,'seconds':extent/RATE,'channels':channels,'master_gain':master_gain,'speech_activity_regions':[list(region) for region in speech_regions],'word_timing_transformed':timing_scaled,'join_edges_checked':True,'assembly_method':'dependency-sequenced independent layers','overlap_policy':'explicit crossfade within fades or complete-clip mix overlap',**timing}
