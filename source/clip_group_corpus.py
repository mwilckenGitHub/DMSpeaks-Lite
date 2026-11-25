"""Source-preserving corpus extension for replacement-group authoring state.

Local Qwen scaffold corrected for root placement, reserved-field collisions,
explicit null, validation order and import cycles. Media is never implied.
"""
import json

EXTENSION='_dmspeaks_clip_groups'

def _location(root,kind,container,rows):
 if kind not in {'json','csv'}:raise ValueError('Unsupported corpus format for replacement groups.')
 if not isinstance(rows,list) or not rows or any(not isinstance(row,dict) for row in rows):raise ValueError('Replacement groups require a nonempty record array.')
 if isinstance(root,dict):
  if kind!='json' or container is None or root.get(container) is not rows:raise ValueError('Invalid corpus record container.')
  return root,EXTENSION
 if not isinstance(root,list) or root is not rows or container is not None:raise ValueError('Invalid corpus record array.')
 return root[0],EXTENSION+('_json' if kind=='csv' else '')

def read(data):
 from clip_groups import validate_config
 owner,key=_location(data['root'],data['kind'],data['container'],data['rows'])
 value=owner[key] if key in owner else {'format':'dmspeaks-clip-groups','version':'0.1','dialogues':[]}
 if data['kind']=='csv' and key in owner:
  if not isinstance(value,str):raise ValueError('CSV replacement groups must contain JSON text.')
  try:value=json.loads(value)
  except ValueError as error:raise ValueError('Malformed CSV replacement group JSON.') from error
 return validate_config(value,len(data['rows']))

def embed(root,config,kind='json',container=None):
 """Mutate an export copy, after validating both new state and reserved fields."""
 from clip_groups import validate_config
 rows=root.get(container) if isinstance(root,dict) else root
 owner,key=_location(root,kind,container,rows)
 normalized=validate_config(config,len(rows))
 # An unrelated or malformed reserved field must never be overwritten.
 read({'root':root,'rows':rows,'kind':kind,'container':container})
 if not normalized['dialogues'] and key not in owner:return root
 owner[key]=json.dumps(normalized,ensure_ascii=False) if kind=='csv' else normalized
 return root
