"""Interpret a bitmap or binary input as a new independent audio track."""
import copy
import hashlib
import math
import re
import uuid
from pathlib import Path
from spectral_tools import RATE, image_audio, raw_audio


def stage(app,content,data,name):
    from background_audio import current_base
    current_base(app,data['asset_id'])
    if not content or len(content)>80_000_000:raise ValueError('Choose a file between one byte and 80 MB.')
    ident=uuid.uuid4().hex;folder=app.folder/'pending-data-imports';folder.mkdir(exist_ok=True)
    path=folder/ident;path.write_bytes(content)
    try:return app.submit('data_audio_import',{**data,'upload_id':ident,'filename':Path(name).name[:240]})
    except Exception:path.unlink(missing_ok=True);raise


def render(app,data,status):
    from background_audio import current_base,save_mix
    from audio import run
    from pcm_input import pcm_input_file
    from engine_limits import MAX_SEGMENTS
    ident=data.get('upload_id','')
    if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Unknown uploaded data.')
    pending=app.folder/'pending-data-imports'/ident
    try:
        base=current_base(app,data['asset_id']);content=pending.read_bytes()
        if len(base['layers'])>=MAX_SEGMENTS:raise ValueError('The arrangement is at its current clip capacity.')
        mode=data.get('mode');name=data.get('name') or data['filename']
        if not isinstance(name,str) or not name.strip() or len(name)>120:raise ValueError('Enter a track name of at most 120 characters.')
        start=data.get('start_sample',0);level=data.get('gain_db',-18)
        if type(start) is not int or not 0<=start<RATE*1800:raise ValueError('Choose a valid timeline start.')
        if isinstance(level,bool) or not isinstance(level,(int,float)) or not math.isfinite(level) or not -60<=level<=12:raise ValueError('Choose a track level from -60 to +12 dB.')
        status('Interpreting image or raw data; no speech model…')
        if mode=='image':pcm,recipe=image_audio(content,data.get('sample_count'),data.get('spectrogram'));channels=1
        elif mode=='raw':
            channels=data.get('channels',1)
            pcm,recipe=raw_audio(content,data.get('encoding','u8'),data.get('sample_rate',RATE),channels,data.get('byte_offset',0))
        else:raise ValueError('Choose image synthesis or raw PCM import.')
        count=len(pcm)//channels
        if count+start>RATE*1800:raise ValueError('This track would exceed the 30-minute editor timeline.')
        source_id=uuid.uuid4().hex;path=app.audio.root/(source_id+'.wav');original=app.audio.root/(source_id+'.input')
        # Float WAV retains interpreted samples above unity. Track/master level
        # controls audition level; importing never silently clips the source.
        with pcm_input_file(pcm) as input_pcm:
            run(['ffmpeg','-v','error','-nostdin','-y','-f','f32le','-ar',str(RATE),'-ac',str(channels),'-i',str(input_pcm),'-c:a','pcm_f32le',str(path)])
        original.write_bytes(content)
        source=app.store.add_asset(dict(kind='imported-data-audio',path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            dataset=base['dataset'],line_id=base['line_id'],record_id=base['record_id'],source_revision=base['source_revision'],
            channels=channels,sample_count=count,sample_rate=RATE,seconds=count/RATE,words=[],timing_source='not_speech',
            data_import=dict(recipe,filename=data['filename'],input_path=str(original),input_sha256=hashlib.sha256(content).hexdigest())))
        layers=copy.deepcopy(base['layers']);tracks=copy.deepcopy(base['tracks']);clip_id='data-'+uuid.uuid4().hex;track_id='import:'+clip_id
        layers.append(dict(id=clip_id,track_id=track_id,clip_role='imported_audio',kind='audio',asset_id=source['id'],
            slot_order=max((l['slot_order'] for l in layers),default=0)+1,slot=dict(type='imported_audio',spoken_text=name),
            after_layer_id=None,anchor_start_sample=start,source_in_sample=0,source_out_sample=count,playback_speed=1,
            fade_in_samples=0,fade_out_samples=0,gain_db=0,gap_samples=0,overlap_samples=0,muted=False,label=name))
        tracks.append(dict(id=track_id,name=name,role='imported_audio',gain_db=level,pan=0,muted=False,solo=False))
        result=save_mix(app,base,layers,tracks,status)
        result['imported_track_id']=track_id
        return app.store.add_asset(result)
    finally:pending.unlink(missing_ok=True)


def bundle_input(archive,item,asset):
    """Keep the uploaded bytes portable without treating them as audio media."""
    recipe=asset.get('data_import')
    if recipe is None:return
    recipe=copy.deepcopy(recipe);content=Path(recipe.pop('input_path')).read_bytes()
    if hashlib.sha256(content).hexdigest()!=recipe['input_sha256']:raise ValueError('Imported input bytes changed; reimport the current file.')
    entry='imports/'+asset['id']+'.input';archive.writestr(entry,content);recipe['input_entry']=entry;item['data_import']=recipe


def validate_inputs(archive,assets):
    for asset in assets:
        recipe=asset.get('data_import')
        if recipe is None:continue
        if not isinstance(recipe,dict):raise ValueError('Invalid data-import recipe.')
        entry=recipe.get('input_entry')
        if not isinstance(entry,str) or not re.fullmatch(r'imports/[a-zA-Z0-9_-]+\.input',entry):raise ValueError('Missing original imported input.')
        try:
            info=archive.getinfo(entry)
        except KeyError:
            raise ValueError('Missing original imported input.') from None
        if not 0<info.file_size<=80_000_000:raise ValueError('Imported input hash does not match the project.')
        with archive.open(info) as source:
            digest=hashlib.file_digest(source,'sha256').hexdigest()
        if digest!=recipe.get('input_sha256'):raise ValueError('Imported input hash does not match the project.')


def restore_input(archive,asset,folder,ident):
    if 'data_import' not in asset:return
    recipe=asset['data_import'];path=folder/(ident+'.input');path.write_bytes(archive.read(recipe.pop('input_entry')));recipe['input_path']=str(path)
