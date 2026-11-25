"""FFT analysis and image sonification. Display colors never alter source audio.

Audio uses the editor's 44100 Hz frame clock. STFT windows are centered on
their timeline samples; dB values are referenced to a full-scale sinusoid.
"""
import array
import math
import struct
import zlib

RATE = 44100
DEFAULTS = dict(scale='log', min_hz=40, max_hz=16000, range_db=90,
                gain_db=0, fft_size=2048, palette='ember')


def settings(value=None):
    value=value or {}
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):raise ValueError('Unknown spectrogram settings.')
    result={**DEFAULTS,**value}
    if result['scale'] not in ('linear','log') or result['palette'] not in ('ember','gray'):raise ValueError('Choose a frequency scale and color scale.')
    for name,low,high in [('min_hz',0,22049),('max_hz',1,22050),('range_db',10,160),('gain_db',-60,60)]:
        n=result[name]
        if isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or not low<=n<=high:raise ValueError('Invalid spectrogram '+name+'.')
    if result['min_hz']>=result['max_hz'] or result['scale']=='log' and result['min_hz']<1:raise ValueError('Choose an increasing frequency range; logarithmic scales start above zero.')
    if type(result['fft_size']) is not int or result['fft_size'] not in (256,512,1024,2048,4096,8192):raise ValueError('Choose an FFT size from 256 to 8192.')
    return result


def frequency_rows(config,height):
    import numpy as np
    c=settings(config)
    return (np.geomspace if c['scale']=='log' else np.linspace)(c['max_hz'],c['min_hz'],height)


def analyze(pcm,channels,width=1024,height=192,config=None,*,interval=None):
    """Return channel-separated, peak-held dBFS images; never downmix stereo."""
    import numpy as np
    c=settings(config)
    if type(width) is not int or not 32<=width<=4096 or type(height) is not int or not 32<=height<=512:raise ValueError('Invalid spectrogram image resolution.')
    if type(channels) is not int or channels not in (1,2) or len(pcm)%channels:raise ValueError('Use mono or stereo audio frames.')
    signal=np.asarray(pcm,dtype=np.float32).reshape(-1,channels)
    if not np.isfinite(signal).all():raise ValueError('Audio contains non-finite samples.')
    start,stop=(0,len(signal)) if interval is None else interval
    if not 0<=start<=stop<=len(signal):raise ValueError('Invalid spectrogram interval.')
    count=stop-start;n=c['fft_size'];hop=n//4;window=np.hanning(n);normal=2/window.sum()
    # Each picture column represents its complete interval. Peak holding keeps
    # brief events visible when a long recording is viewed at a small width.
    output=np.zeros((channels,height,width),dtype=np.float32)
    bins=np.fft.rfftfreq(n,1/RATE);rows=frequency_rows(c,height)
    row_for_bin=np.abs(rows[:,None]-bins[None,:]).argmin(axis=0)
    usable=np.flatnonzero((bins>=c['min_hz'])&(bins<=c['max_hz']))
    padded=np.pad(signal,((n//2,n//2),(0,0)))
    for first in range(start,stop,hop*64):
        centers=np.arange(first,min(stop,first+hop*64),hop)
        frames=padded[centers[:,None]+np.arange(n)[None,:],:]
        spectra=np.abs(np.fft.rfft(frames*window[None,:,None],axis=1))*normal
        columns=np.minimum(width-1,(centers-start)*width//count)
        for i,column in enumerate(columns):
            for channel in range(channels):
                interpolated=np.interp(rows[::-1],bins,spectra[i,:,channel])[::-1]
                output[channel,:,column]=np.maximum(output[channel,:,column],interpolated)
                np.maximum.at(output[channel,:,column],row_for_bin[usable],spectra[i,usable,channel])
    # When zoomed beyond the hop size, reuse the nearest measured time column.
    if count:
        measured=np.unique(np.minimum(width-1,np.arange(0,count,hop)*width//count))
        nearest=measured[np.minimum(len(measured)-1,np.searchsorted(measured,np.arange(width)))]
        missing=np.setdiff1d(np.arange(width),measured);output[:,:,missing]=output[:,:,nearest[missing]]
    return 20*np.log10(np.maximum(output,1e-12)),c


def png(db,config):
    """Encode true-color PNG using only the standard library after analysis."""
    import numpy as np
    c=settings(config);a=np.asarray(db);channels,height,width=a.shape
    normalized=np.clip((a+c['gain_db']+c['range_db'])/c['range_db'],0,1)
    stops=np.array([[0,0,0],[18,10,65],[102,24,121],[203,48,65],[255,159,43],[255,255,225]],float) if c['palette']=='ember' else np.array([[0,0,0],[255,255,255]],float)
    positions=np.linspace(0,1,len(stops));pixels=np.stack([np.interp(normalized,positions,stops[:,i]) for i in range(3)],axis=-1).astype('uint8').reshape(channels*height,width,3)
    def chunk(kind,body):return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body)&0xffffffff)
    raw=b''.join(b'\0'+row.tobytes() for row in pixels)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,channels*height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


def image_audio(content,frames,config=None):
    """Map image luminance to spectral magnitude with deterministic phase.

    The image is resampled across exactly `frames` and the configured frequency
    range. Phase-coherent overlap-add produces an independent mono clip.
    """
    import numpy as np
    import tempfile
    from pathlib import Path
    from audio import run
    c=settings(config)
    if not (content.startswith(b'\x89PNG\r\n\x1a\n') or content.startswith(b'BM')):raise ValueError('Choose a PNG or BMP image.')
    if type(frames) is not int or not 1<=frames<=RATE*1800:raise ValueError('Choose an image duration between one frame and 30 minutes.')
    n=c['fft_size'];hop=n//4;columns=min(4096,max(1,math.ceil(frames/hop)+1));rows=min(512,n//2+1)
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'image';path.write_bytes(content)
        # ffmpeg scales the image and decodes alpha against black. No executable
        # file is ever run and neither the input nor an existing clip is altered.
        raw=run(['ffmpeg','-v','error','-nostdin','-i',str(path),'-vf',f'format=gbrap,premultiply=inplace=1,scale={columns}:{rows},format=rgba','-frames:v','1','-f','rawvideo','-pix_fmt','rgba','-'])
    if len(raw)!=rows*columns*4:raise ValueError('The image could not be decoded as a single bitmap.')
    pixels=np.frombuffer(raw,dtype=np.uint8).reshape(rows,columns,4).astype(float)/255
    image=pixels[:,:,:3]@np.array([.2126,.7152,.0722])
    targets=frequency_rows(c,rows)[::-1];bins=np.fft.rfftfreq(n,1/RATE);window=np.hanning(n)
    result=np.zeros(frames+n,dtype=float);weights=np.zeros_like(result);phase=np.random.default_rng(731).uniform(-np.pi,np.pi,len(bins))
    for column,start in enumerate(range(-n//2,frames,hop)):
        x=min(columns-1,max(0,round(max(0,start+n//2)/max(1,frames-1)*(columns-1))))
        magnitudes=np.interp(bins,targets,image[::-1,x],left=0,right=0)
        spectrum=magnitudes*np.exp(1j*(phase+2*np.pi*np.arange(len(bins))*start/n))
        block=np.fft.irfft(spectrum,n)*window
        lo=max(0,start);hi=min(frames,start+n);section=slice(lo-start,hi-start)
        result[lo:hi]+=block[section];weights[lo:hi]+=window[section]
    result=result[:frames]/np.maximum(weights[:frames],1e-12);peak=np.max(np.abs(result))
    if peak:result*=10**(-6/20)/peak
    return array.array('f',result.astype('float32')),dict(method='image-luminance-inverse-stft',settings=c,peak_dbfs=-6,sample_count=frames)


def raw_audio(content,encoding='u8',rate=44100,channels=1,offset=0):
    """Interpret bytes as PCM explicitly; headers/text are ordinary sample data."""
    import numpy as np
    import tempfile
    from pathlib import Path
    from audio import run
    formats={'u8':1,'s8':1,'s16le':2,'s16be':2,'s24le':3,'s24be':3,'s32le':4,'s32be':4,'f32le':4,'f32be':4,'f64le':8,'f64be':8}
    if encoding not in formats or type(rate) is not int or not 1000<=rate<=192000 or type(channels) is not int or channels not in (1,2):raise ValueError('Choose a supported PCM encoding, sample rate and mono/stereo channels.')
    if type(offset) is not int or not 0<=offset<len(content):raise ValueError('Byte offset must leave sample data.')
    payload=content[offset:];size=formats[encoding]*channels
    if len(payload)%size:raise ValueError('The bytes after the offset must form complete PCM frames. Change the format, channels or offset.')
    if len(payload)//size>rate*1800:raise ValueError('Raw audio exceeds 30 minutes at this sample rate.')
    if encoding.startswith('f'):
        dtype=('<' if encoding.endswith('le') else '>')+('f4' if encoding.startswith('f32') else 'f8')
        if not np.isfinite(np.frombuffer(payload,dtype=dtype)).all():raise ValueError('The chosen floating-point format contains non-finite values. Choose the intended encoding.')
    with tempfile.TemporaryDirectory() as folder:
        source=Path(folder)/'data';source.write_bytes(payload)
        raw=run(['ffmpeg','-v','error','-nostdin','-f',encoding,'-ar',str(rate),'-ac',str(channels),'-i',str(source),'-ar',str(RATE),'-ac',str(channels),'-f','f32le','-'])
    pcm=array.array('f');pcm.frombytes(raw)
    if not pcm or not all(math.isfinite(v) for v in pcm):raise ValueError('The selected raw format did not produce finite audio.')
    return pcm,dict(method='raw-pcm',encoding=encoding,source_sample_rate=rate,channels=channels,byte_offset=offset,source_frame_count=len(payload)//size)
