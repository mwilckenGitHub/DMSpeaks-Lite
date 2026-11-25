import copy
from engine_limits import silence_duration

def remap_workspace(workspace, id_map, dataset):
    if workspace is None:
        workspace = {}
    validate_workspace(workspace)

    if 'selections' in workspace:
        sel = workspace['selections']
        if not isinstance(sel, dict):
            raise ValueError("selections must be a dict")
        for k, v in sel.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("selections keys and values must be strings")

    result = copy.deepcopy(workspace)
    result['dataset'] = dataset

    if 'asset_id' in result:
        old_id = result['asset_id']
        if old_id is not None:
            result['asset_id'] = id_map.get(old_id)
        else:
            result['asset_id'] = None

    if 'selections' in result:
        new_sel = {}
        for k, v in result['selections'].items():
            if v == 'silence':
                new_sel[k] = v
            else:
                new_id = id_map.get(v)
                if new_id is not None:
                    new_sel[k] = new_id
        result['selections'] = new_sel

    if 'entry_audio' in result:
        result['entry_audio'] = {
            f"{dataset}:{key.rsplit(':', 1)[1]}": id_map[asset_id]
            for key, asset_id in result['entry_audio'].items()
            if key.rsplit(':', 1)[0] == workspace.get('dataset') and asset_id in id_map
        }

    return result

def validate_workspace(workspace, row_count=None):
    """Validate saved UI state before any portable-project writes."""
    import math
    if workspace is None:
        return
    if not isinstance(workspace, dict):
        raise ValueError('Invalid workspace: expected an object.')
    for key in ('dataset','provider','voice','variant','reference_id','character_id'):
        if key in workspace and not isinstance(workspace[key], str):
            raise ValueError('Invalid workspace '+key+'.')
    if 'player' in workspace:
        if not isinstance(workspace['player'], dict) or any(not isinstance(k,str) or not isinstance(v,(str,int,float,bool,type(None))) or isinstance(v,float) and not math.isfinite(v) for k,v in workspace['player'].items()):
            raise ValueError('Invalid workspace test values.')
    if 'timeline_view' in workspace and workspace['timeline_view'] not in ('layers','mixed','both'):raise ValueError('Invalid timeline view.')
    if 'stage_directions' in workspace and workspace['stage_directions'] not in ('auto','wow','preserve'):raise ValueError('Invalid stage direction mode.')
    if 'layer_layout_initialized' in workspace and not isinstance(workspace['layer_layout_initialized'],bool):raise ValueError('Invalid layer layout flag.')
    if 'index' in workspace:
        value=workspace['index']
        if not isinstance(value,int) or isinstance(value,bool) or value<0 or row_count is not None and value>=row_count:
            raise ValueError('Invalid workspace dialogue index.')
    for key,lower,upper in [('zoom',1,16384),('playback_rate',.75,2.5),('rate',.1,4),('seed',0,2**32-1)]:
        if key not in workspace:continue
        value=workspace[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lower<=value<=upper:
            raise ValueError('Invalid workspace '+key+'.')
    for key in ('silence_ms', 'missing_silence_ms'):
        if key in workspace:
            silence_duration(workspace[key], 'Workspace '+key)
            if key == 'missing_silence_ms' and workspace[key] == 0:
                raise ValueError('Remembered silence must be positive; use silence_ms=0 to skip.')
    if workspace.get('asset_id') is not None and not isinstance(workspace['asset_id'],str):
        raise ValueError('Invalid workspace audio ID.')
    if 'audio_export' in workspace:
        validate_audio_export(workspace['audio_export'])
    if 'selections' in workspace and (not isinstance(workspace['selections'],dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in workspace['selections'].items())):
        raise ValueError('Invalid workspace clip selections.')
    if 'entry_audio' in workspace:
        if not isinstance(workspace['entry_audio'], dict):
            raise ValueError('Invalid workspace entry audio.')
        for key, asset_id in workspace['entry_audio'].items():
            owner, _, index = key.rpartition(':') if isinstance(key, str) else ('', '', '')
            if (not owner or not index.isascii() or not index.isdigit() or len(index)>10
                    or str(int(index)) != index or not isinstance(asset_id, str) or not asset_id):
                raise ValueError('Invalid workspace entry audio.')
            if row_count is not None and (owner != workspace.get('dataset') or int(index) >= row_count):
                raise ValueError('Workspace entry audio is outside this corpus.')


def validate_audio_export(settings):
    from audio import RATES

    choices = {
        'format': ('ogg', 'wav', 'mp3', 'flac'),
        'rate': RATES,
        'channels': (1, 2),
        'quality': range(11),
        'bitrate': (32, 48, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
        'depth': (16, 24, 32),
    }
    if not isinstance(settings, dict) or settings.keys() != choices.keys():
        raise ValueError('Invalid project audio export settings.')
    for key, values in choices.items():
        expected_type = str if key == 'format' else int
        if type(settings[key]) is not expected_type or settings[key] not in values:
            raise ValueError(f'Invalid project audio export {key}.')
    if settings['format'] == 'flac' and settings['depth'] == 32:
        raise ValueError('FLAC export supports 16-bit or 24-bit depth.')
