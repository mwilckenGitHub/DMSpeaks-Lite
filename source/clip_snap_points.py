"""Non-destructive clip guides; measured activity is not a phoneme label.

Positions use the rendered clip's sample clock. Original provider times remain
provenance. Coincident guides remain distinct; absent data is never invented.
"""
import array
import collections
import hashlib
import math
import sys

from timing_summary import has_partial_timing


def minimum_activity_level(layer, edge):
    """Return the configured RMS threshold, or None for automatic."""
    value = layer.get(edge + '_min_dbfs')
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -100 <= value <= 0:
        raise ValueError(f'{edge.title()} minimum level must be between -100 and 0 dBFS.')
    return 10 ** (value / 20)


def sustained_activity(levels, threshold, minimum):
    first = last = None
    start = None
    for index, level in enumerate(levels):
        if level >= threshold:
            if start is None:
                start = index
            if index - start + 1 >= minimum:
                if first is None:
                    first = start
                last = index
        else:
            start = None
    return first, last


def word_belongs_to_clip(layer, word):
    """Keep a timestamp on its owned clip when the source order is known."""
    order = word.get('segment_order')
    slot = layer.get('slot_order')
    ident = layer.get('id', '')
    if not ident.startswith('packet:') or order is None or type(slot) is not int:
        return True
    role = ident.rsplit(':', 1)[-1]
    if role == 'word':
        return order == slot
    if role == 'before':
        return order != slot + 1
    if role == 'after':
        return order != slot - 1
    return True


def clip_snap_points(pcm, channels, rate, layer, asset=None, *, activity_cache=None):
    count=len(pcm)//channels
    points=[{'id':'clip_start','kind':'clip','side':'start','sample':0,'label':'Clip start'},
            {'id':'clip_end','kind':'clip','side':'end','sample':count,'label':'Clip end'}]
    asset=asset or {}
    speed=layer.get('effective_speed',1)
    trim=layer.get('source_in_sample',0)/rate
    offset=layer.get('word_offset_samples',layer.get('rendered_region',{}).get('start_sample',0))/rate
    dry=layer.get('dry_rendered_sample_count',count)
    timed=[]
    for word in asset.get('words') or []:
        if not word_belongs_to_clip(layer, word):continue
        a=(word['start']-trim)/speed-offset
        b=(word['end']-trim)/speed-offset
        # A neighboring word ending on the first retained sample owns none
        # of this clip. Keep collapsed observations strictly inside the clip.
        if round(b*rate)<=0 or round(a*rate)>=dry or b<a:continue
        timed.append((a,b,word))
    if timed:
        first=min(timed,key=lambda item:item[0]);last=max(reversed(timed),key=lambda item:item[1])
        for side,item,index in [('start',first,0),('end',last,1)]:
            if item[0] == item[1]:
                continue
            raw=item[index];position=round(raw*rate)
            points.append({'id':'provider_'+side,'kind':'provider','side':side,
                           'sample':max(0,min(dry,position)),
                           'label':'Current word '+side,
                           'source':asset.get('timing_source','unspecified'),
                           'coordinate_role':'working_word_times',
                           'timing_partial':has_partial_timing(asset),
                           'source_word':item[2].get('word',''),
                           'original_word_seconds':[item[2]['start'],item[2]['end']],
                           'clipped_to_retained_audio':not 0<=position<=dry})
    from timing_evidence import returned_groups
    for channel,words,origin,observation in returned_groups(asset):
        metadata=observation.get('metadata')
        if not isinstance(metadata,dict):metadata={}
        returned=[]
        for word in words:
            a=(word['start']-origin-trim)/speed-offset
            b=(word['end']-origin-trim)/speed-offset
            if round(b*rate)<=0 or round(a*rate)>=dry:continue
            returned.append((a,b,word))
        if not returned:continue
        # On tied end times the last reported word owns the edge. A collapsed
        # suffix can share the previous word's endpoint on a truncated return.
        first=min(returned,key=lambda item:item[0]);last=max(reversed(returned),key=lambda item:item[1])
        for side,item,index in [('start',first,0),('end',last,1)]:
            # Keep collapsed words in evidence, but never snap to them or
            # silently substitute a different word at the edge.
            if item[0] == item[1]:
                continue
            position=round(item[index]*rate)
            points.append({'id':'returned_'+channel+'_'+side,'kind':'returned','side':side,
                           'sample':max(0,min(dry,position)),
                           'label':'Returned '+channel+' '+side,
                           'source':observation.get('timing_source','unspecified'),
                           'coordinate_role':'retained_returned_word_times',
                           'source_word':item[2]['word'],'original_word_seconds':[item[2]['start'],item[2]['end']],
                           'observation_audio_sha256':observation.get('audio_sha256'),
                           'timestamp_resolution_seconds':metadata.get('timestamp_resolution_seconds'),
                           'timing_partial':metadata.get('timing_partial') is True,
                           'source_offset_seconds':origin,'clipped_to_retained_audio':not 0<=position<=dry,
                           'acoustic_accuracy_verified':False})
    raw=pcm if isinstance(pcm,array.array) and pcm.typecode=='f' else array.array('f',pcm)
    if sys.byteorder!='little':raw=array.array('f',raw);raw.byteswap()
    pcm_digest=hashlib.sha256(memoryview(raw).cast('B')).hexdigest()
    minima=tuple(minimum_activity_level(layer, edge) for edge in ('leader','decay'))
    # Only activity depends entirely on the PCM. Word guides also depend on
    # current source timing and ownership, so they are rebuilt above every time.
    # Hash original float32 frames only when their representation is exact.
    cache=activity_cache if isinstance(pcm,array.array) and pcm.typecode=='f' else None
    key=(pcm_digest,channels,rate,minima)
    activity=cache.get(key) if cache is not None else None
    if activity is None:
        activity=activity_points(pcm,channels,rate,minima)
        if cache is not None:
            if len(cache)>=256:
                del cache[next(iter(cache))]
            cache[key]=activity
    points.extend(point.copy() for point in activity)
    return {'format':'dmspeaks-clip-snap-points','version':'0.1',
            'sample_rate':rate,'sample_count':count,'audio_unchanged':True,
            'source_audio_sha256':asset.get('sha256'),
            'analysis_pcm_sha256':pcm_digest,
            'analysis_sample_format':'float32le',
            'analysis_stage':'rendered clip before track level and master gain',
            'points':points}


def activity_points(pcm, channels, rate, minima):
    """Measure waveform activity with a bounded rolling RMS window."""
    count=len(pcm)//channels
    window=max(1,round(rate*.01));hop=max(1,round(rate*.001))
    queue=collections.deque();power=0.;levels=array.array('f')
    for frame in range(count):
        value=sum(float(pcm[frame*channels+c])**2 for c in range(channels))/channels
        if not math.isfinite(value):raise ValueError('Snap analysis requires finite PCM.')
        queue.append(value);power+=value
        if len(queue)>window:power-=queue.popleft()
        if len(queue)==window and (frame-window+1)%hop==0:
            levels.append(math.sqrt(max(0,power)/window))
    peak=max(levels,default=0)
    automatic=max(.001,peak*.01)
    minimum=max(1,math.ceil(.008*rate/hop))
    points=[];extents={}
    for index,(ident,side,edge,edge_sample) in enumerate([
            ('leader','start',0,0),('decay','end',len(levels)-1,count)]):
        override=minima[index]
        threshold=automatic if override is None else override
        if threshold not in extents:
            extents[threshold]=sustained_activity(levels,threshold,minimum)
        extent=extents[threshold][index]
        if extent is None:
            continue
        touches=extent==edge
        position=edge_sample if touches else min(count,extent*hop+window//2)
        points.append({'id':ident,'kind':'activity','side':side,'sample':position,
                       'label':ident.title()+' estimate',
                       'method':'10ms RMS, approximately1ms steps, sustained8ms activity',
                       'threshold_rms':threshold,'threshold_dbfs':20*math.log10(threshold),
                       'threshold_mode':'custom' if override is not None else 'automatic',
                       'touches_clip_edge':touches,'review_required':True,'phoneme_boundary_claimed':False})
    return points
