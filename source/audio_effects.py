"""Deterministic nondestructive clip effects; durations use frames per channel."""
import array,math,random,tempfile
from pathlib import Path
from audio import run
from pcm_input import pcm_input_file
from effects_schema import validate_effects
from stereo_frames import frame_count
RATE=44100

def _edge_fade(pcm,channels,milliseconds,out=False):
 """Raised-cosine envelope preserves all source frames, including long tails."""
 result=array.array('f',pcm);frames=frame_count(result,channels);count=min(frames,round(milliseconds*RATE/1000))
 for i in range(count):
  gain=math.sin(math.pi/2*i/max(1,count-1))**2
  frame=frames-1-i if out else i
  for channel in range(channels):result[frame*channels+channel]*=gain
 return result

def _filter(pcm,channels,expression):
 with pcm_input_file(pcm) as source:
  raw=run(['ffmpeg','-v','error','-nostdin','-f','f32le','-ar',str(RATE),'-ac',str(channels),'-i',str(source),'-af',expression,'-f','f32le','-acodec','pcm_f32le','-'])
 result=array.array('f');result.frombytes(raw);return result

def _reverb(pcm,channels,tail_ms,wet):
 """A fixed synthetic room impulse, mixed in parallel with unchanged dry PCM."""
 count=frame_count(pcm,channels);tail=round(tail_ms*RATE/1000);ir=array.array('f',[0])*tail;rng=random.Random(9231+tail);smooth=0;lead=min(round(.012*RATE),tail//4)
 for i in range(lead,tail):
  smooth=.7*smooth+.3*rng.uniform(-1,1);ir[i]=smooth*(1-i/max(1,tail-1))**3
 energy=math.sqrt(sum(x*x for x in ir)) or 1;ir=array.array('f',(v/energy for v in ir));padded=array.array('f',pcm);padded.extend(array.array('f',[0])*(tail*channels))
 with tempfile.TemporaryDirectory() as folder:
  source=Path(folder)/'source.f32';impulse=Path(folder)/'impulse.f32';source.write_bytes(padded.tobytes());impulse.write_bytes(ir.tobytes())
  raw=run(['ffmpeg','-v','error','-nostdin','-f','f32le','-ar',str(RATE),'-ac',str(channels),'-i',str(source),'-f','f32le','-ar',str(RATE),'-ac','1','-i',str(impulse),'-filter_complex','[0:a][1:a]afir=dry=1:wet=1:gtype=none:irfmt=mono:minp=64:maxp=1024:precision=float[out]','-map','[out]','-f','f32le','-acodec','pcm_f32le','-'])
 reflected=array.array('f');reflected.frombytes(raw)
 if len(reflected)!=len(padded):raise ValueError('Room convolution changed the expected padded frame count.')
 return array.array('f',(dry+wet*room for dry,room in zip(padded,reflected)))

def apply_effects(pcm,channels,effects):
 nodes=validate_effects(effects);count=frame_count(pcm,channels);result=array.array('f',pcm)
 for node in nodes:
  if not node['enabled'] or not result:continue
  kind=node['type'];p=node['params']
  if kind=='normalize_peak':
   peak=max((abs(value) for value in result),default=0)
   if peak:result=array.array('f',(value*(10**(p['target_db']/20)/peak) for value in result))
  elif kind=='invert_polarity':result=array.array('f',(-value for value in result))
  elif kind=='fade_in':result=_edge_fade(result,channels,p['duration_ms'])
  elif kind=='fade_out':result=_edge_fade(result,channels,p['duration_ms'],True)
  elif kind=='highpass':result=_filter(result,channels,f"highpass=f={p['cutoff_hz']}")
  elif kind=='lowpass':result=_filter(result,channels,f"lowpass=f={p['cutoff_hz']}")
  elif kind=='equalizer':result=_filter(result,channels,f"equalizer=f={p['frequency_hz']}:t=q:w={p['q']}:g={p['gain_db']}")
  elif kind=='compressor':result=_filter(result,channels,f"acompressor=threshold={10**(p['threshold_db']/20)}:ratio={p['ratio']}:attack={p['attack_ms']}:release={p['release_ms']}:makeup={10**(p['makeup_db']/20)}:link=maximum")
  elif kind=='delay' and p['wet']>0:result=_filter(result,channels,f"aecho=1:1:{p['delay_ms']}:{p['wet']}")
  elif kind=='chorus' and p['wet']>0:result=_filter(result,channels,f"chorus=1:1:{p['delay_ms']}:{p['wet']}:{p['rate_hz']}:{p['depth_ms']}")
  elif kind=='reverb' and p['wet']>0:result=_reverb(result,channels,p['tail_ms'],p['wet'])
  if frame_count(result,channels)<count:raise ValueError('An effect shortened the selected speech; bypass it and review the source.')
  if any(not math.isfinite(x) for x in result):raise ValueError('An effect produced invalid samples.')
 return result,{'effects':nodes,'dry_rendered_sample_count':count,'effect_tail_sample_count':frame_count(result,channels)-count,'effects_engine':'ffmpeg filters; deterministic synthetic room convolution; dmspeaks-fx-0.1'}
