"""Post-effect, post-fader track signals on the common arrangement clock."""
import array
from layered_audio import render_layers


def render_track_outputs(layers,tracks,store,*,audition_only=False):
    """Return metadata and a single-use iterator; never double-mix bus members."""
    stems={};buses={}
    mixed,meta=render_layers(layers,tracks,store,stems,audition_only=audition_only,bus_stems=buses)
    del mixed
    channels=meta['channels'];length=meta['sample_count']*channels
    def outputs():
        for track in meta['tracks']:
            signal=array.array('f',[0])*length
            for layer in meta['layers']:
                if layer['track_id']!=track['id']:continue
                source=stems.pop(layer['id'])
                if layer.get('track_bus_id'):continue
                start=layer['start_sample']*channels
                for i,value in enumerate(source):signal[start+i]+=value
            for bus in meta['track_buses']:
                if bus['track_id']!=track['id']:continue
                start=bus['start_sample']*channels
                for i,value in enumerate(buses.pop(bus['id'])):signal[start+i]+=value
            yield track,signal
    return meta,outputs()
