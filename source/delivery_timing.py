"""Measure decoded delivery frames without retaining complete PCM in memory."""
import subprocess,tempfile
from pathlib import Path
from audio import probe

def measure_delivery(path):
 info=probe(path);rate=info['sample_rate'];channels=info['channels']
 from vorbis_pcm import is_vorbis,measure_vorbis
 if is_vorbis(path):
  frames,rate,channels=measure_vorbis(path)
 else:
  # Use a disk-backed sink, not capture_output, for potentially long stereo files.
  with tempfile.TemporaryFile() as decoded:
   result=subprocess.run(['ffmpeg','-v','error','-nostdin','-i',str(Path(path)),'-vn','-ar',str(rate),'-ac',str(channels),'-f','f32le','-acodec','pcm_f32le','-'],stdout=decoded,stderr=subprocess.PIPE,timeout=180,
       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
   if result.returncode:raise ValueError('Cannot measure exported audio: '+result.stderr.decode(errors='replace')[-500:])
   count=decoded.tell();frame_bytes=4*channels
  if count%frame_bytes:raise ValueError('Exported audio has an invalid decoded frame count.')
  frames=count//frame_bytes
 if not frames:raise ValueError('Exported audio has an invalid decoded frame count.')
 return {'audio_sample_count':frames,'audio_sample_rate':rate,'audio_channels':channels,'audio_seconds':frames/rate,'audio_container_seconds':info['seconds'],'audio_codec':info['codec'],'audio_duration_basis':'decoded delivery frames; samples are per channel'}
