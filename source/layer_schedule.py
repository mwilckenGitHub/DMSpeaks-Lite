"""Shared-clock dependencies across tracks; supervised and corrected local Qwen draft."""
from engine_limits import MAX_SEGMENTS
import copy
from guide_bindings import binding,parent_id,resolve
from replacement_snap import join as replacement_join

def schedule_layers(layers):
 if not isinstance(layers,list) or len(layers)>MAX_SEGMENTS:raise ValueError(f'Use an array of at most {MAX_SEGMENTS} speech layers for this runtime.')
 items={}
 for layer in layers:
  if not isinstance(layer,dict) or not isinstance(layer.get('id'),str) or not layer['id'] or layer['id'] in items:raise ValueError('Layer IDs must be unique nonempty strings.')
  for field in ['rendered_sample_count','anchor_start_sample','gap_samples','overlap_samples','fade_in_samples','fade_out_samples','edge_fade_in_samples','edge_fade_out_samples']:
   value=layer.get(field,0)
   if not isinstance(value,int) or isinstance(value,bool) or value<0:raise ValueError('Invalid layer '+field+'.')
  if layer.get('overlap_mode','crossfade') not in ('crossfade','mix'):raise ValueError('Choose crossfade or complete-clip mix overlap.')
  items[layer['id']]=layer
 resolved={};visiting=set()
 def visit(ident):
  if ident in resolved:return resolved[ident]
  if ident in visiting:raise ValueError('Layer dependencies contain a cycle.')
  if ident not in items:raise ValueError('A layer dependency refers to an unknown clip.')
  visiting.add(ident);layer=items[ident];link=binding(layer);parent=parent_id(layer);overlap=0;evidence=None;snap=None
  if link:
   prior=visit(parent)
   start,evidence=resolve(layer,prior)
   overlap=max(0,min(start+layer.get('rendered_sample_count',0),prior['end_sample'])-max(start,prior['start_sample']))
   evidence['overlap_policy']='Complete clips mix at their authored positions; no automatic fades.'
  elif parent is not None:
   if not isinstance(parent,str):raise ValueError('Invalid preceding layer ID.')
   prior=visit(parent)
   overlap=min(layer.get('overlap_samples',0),prior['rendered_sample_count'],layer.get('rendered_sample_count',0))
   # Complete pronunciations may sound together without attenuating their
   # phonemes. Crossfade remains the default for existing arrangements.
   if layer.get('overlap_mode','crossfade')=='crossfade':overlap=min(overlap,prior.get('edge_fade_out_samples',prior.get('fade_out_samples',0)),layer.get('edge_fade_in_samples',layer.get('fade_in_samples',0)))
   start=prior['end_sample']+layer.get('gap_samples',0)-overlap
   snapped=replacement_join(prior,layer,items)
   if snapped is not None:start,snap=snapped
  else:start=layer.get('anchor_start_sample',0)
  result={**copy.deepcopy(layer),'start_sample':start,'end_sample':start+layer.get('rendered_sample_count',0),'effective_overlap_samples':overlap}
  result.pop('replacement_snap_resolution',None)
  if snap is not None:result['replacement_snap_resolution']=snap
  result.pop('guide_binding_resolution',None)
  if evidence is not None:result['guide_binding_resolution']=evidence
  resolved[ident]=result;visiting.remove(ident);return result
 return [visit(layer['id']) for layer in layers]
