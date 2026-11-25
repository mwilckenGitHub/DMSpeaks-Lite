"""Provider-neutral ordered clip recipes and a reference resolver (not a game API)."""
from engine_limits import MAX_SEGMENTS, silence_duration
import copy,unicodedata,re
from fractions import Fraction
from model import token_value
from layer_schedule import schedule_layers
CLOCK_RATE=44100
class UnavailableSequence(ValueError):
 """The chosen profile lacks fixed speech or any audible-duration content."""

def sequence_fields(segment,previous):
 return {'layer_id':'segment-'+str(segment['order']).zfill(2),'track_id':'fixed' if segment['type']=='fixed' else 'variable:'+segment['type'],'after_layer_id':previous,'anchor_start_sample':0,'gap_samples':0,'overlap_samples':0,'fade_in_samples':0,'fade_out_samples':0,'value_match':'unicode_nfc_casefold' if re.fullmatch(r'\$[nNcCrR]|\$[gG][^:;]*:[^;]*;',segment.get('placeholder','')) else 'unicode_nfc','missing_policy':'skip' if segment.get('speech_excluded') else ('skip' if not segment.get('spoken_text') else 'error') if segment['type']=='fixed' else 'silence','fallback_silence_ms':0 if segment['type']=='fixed' or segment.get('speech_excluded') else 200}

def assembly_policy():
 return {'format':'dmspeaks-clip-sequence','version':'0.1','clock_sample_rate':CLOCK_RATE,'time_unit':'samples_per_channel','completion_rule':'after_previous_clip','duration_basis':'decoded selected delivery audio','overlap_policy':'explicit overlap limited to both fade envelopes','selection_mode':'player_variables','source_audio_processing':'Each candidate contains its effective occurrence-group effects and individual override, as recorded by clip_processing. Non-Vorbis sources are converted whole to Vorbis. Existing Vorbis with no active effects is copied unchanged. Play delivery files at unity without reapplying the baked chain; arrangement-only edits are separate.','word_timing_notice':'Sidecar word timings retain their original provenance; they are not decoder completion callbacks.'}

def match_key(value,mode='unicode_nfc'):
 if mode not in {'unicode_nfc','unicode_nfc_casefold'}:raise ValueError('Unknown variable matching policy.')
 normalized=unicodedata.normalize('NFC',str(value))
 return normalized.casefold() if mode=='unicode_nfc_casefold' else normalized

def _match(a,b,mode='unicode_nfc'):
 return match_key(a,mode)==match_key(b,mode)

def resolve_sequence(entry,player,variant='default',reference_id=None,generation_key=None,silence_ms=None):
 """Resolve installed clip choices, then sequence all tracks by actual lengths.

 No invented names, first-name fallback, networking, or file IO. A host adapter
 still owns playback and must verify its own decoder's completion behavior.
 """
 policy=entry.get('assembly',{})
 if policy.get('format')!='dmspeaks-clip-sequence' or policy.get('version')!='0.1':raise ValueError('This manifest has no supported clip sequence recipe.')
 rate=policy.get('clock_sample_rate',CLOCK_RATE)
 if type(rate) is not int or not 8000<=rate<=192000:raise ValueError('Invalid sequence clock sample rate.')
 if silence_ms is not None:silence_ms=silence_duration(silence_ms)
 segments=entry['segments']
 if not isinstance(segments,list) or not 1<=len(segments)<=MAX_SEGMENTS:raise ValueError(f'Use 1–{MAX_SEGMENTS} ordered segments for this runtime.')
 orders=[segment.get('order') for segment in segments]
 if any(type(order) is not int for order in orders) or orders!=list(range(1,len(segments)+1)):raise ValueError('Segment order must be consecutive from 1.')
 # Choose a consistent reference/performance group before resolving placeholders.
 # A missing name from that group stays silent; another actor's name is never used.
 candidates=[]
 for segment in sorted(segments,key=lambda s:s['type']!='fixed'):
  value=None if segment['type']=='fixed' else token_value(segment['placeholder'],player)[1]
  candidates.extend(c for c in segment.get('clips',[]) if c.get('export_status')=='available' and c.get('clip_variant','default')==variant and (reference_id is None or c.get('clip_reference_id','')==reference_id) and (generation_key is None or c.get('clip_generation_key','')==generation_key) and (segment['type']=='fixed' or value not in ('',None) and _match(c.get('player_value'),value,segment.get('value_match','unicode_nfc'))))
 if candidates:
  if reference_id is None:reference_id=candidates[0].get('clip_reference_id','')
  if generation_key is None:generation_key=next((c.get('clip_generation_key','') for c in candidates if c.get('clip_reference_id','')==reference_id),'')
 prepared=[]
 for segment in segments:
  if not isinstance(segment.get('layer_id'),str) or not isinstance(segment.get('track_id'),str):raise ValueError('Each segment needs a layer and track ID.')
  value=None
  if segment['type']!='fixed':_,value=token_value(segment['placeholder'],player)
  options=[c for c in segment.get('clips',[]) if c.get('export_status')=='available' and c.get('clip_variant','default')==variant and (reference_id is None or c.get('clip_reference_id','')==reference_id) and (generation_key is None or c.get('clip_generation_key','')==generation_key) and (segment['type']=='fixed' or value not in ('',None) and _match(c.get('player_value'),value,segment.get('value_match','unicode_nfc')))]
  chosen=options[0] if options else None
  if chosen:
   frames=chosen.get('audio_sample_count');source_rate=chosen.get('audio_sample_rate')
   if type(frames) is not int or frames<=0 or type(source_rate) is not int or not 8000<=source_rate<=192000:raise ValueError('A selected clip has no valid measured delivery duration.')
   length=round(Fraction(frames*rate,source_rate));kind='audio'
  elif segment.get('missing_policy')=='skip':length=0;kind='direction'
  else:
   if segment.get('missing_policy','error' if segment['type']=='fixed' else 'silence')!='silence':raise UnavailableSequence('Missing fixed speech for segment '+str(segment['order'])+'.')
   milliseconds=segment.get('fallback_silence_ms',200) if silence_ms is None else silence_ms
   milliseconds=silence_duration(milliseconds, 'Placeholder silence duration')
   length=round(Fraction(milliseconds*rate,1000));kind='silence'
  layer={'id':segment['layer_id'],'track_id':segment['track_id'],'slot_order':segment['order'],'after_layer_id':segment.get('after_layer_id'),'rendered_sample_count':length,'kind':kind,'clip':copy.deepcopy(chosen),'resolved_value':value}
  layer.update({key:segment.get(key,0) for key in ['anchor_start_sample','gap_samples','overlap_samples','fade_in_samples','fade_out_samples']});prepared.append(layer)
 scheduled=schedule_layers(prepared)
 return {'clock_sample_rate':rate,'selection_context':{'variant':variant,'reference_id':reference_id,'generation_key':generation_key},'layers':scheduled,'sample_count':max((l['end_sample'] for l in scheduled),default=0),'missing_orders':[l['slot_order'] for l in scheduled if l['kind']=='silence']}
