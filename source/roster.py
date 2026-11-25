"""Offline character profiles are private project state, never shipped corpus data."""
import hashlib,json

def import_roster(value):
 if isinstance(value,dict):value=value.get('characters')
 if not isinstance(value,list) or len(value)>500:raise ValueError('Use a JSON array, or {characters: [...]}, with at most 500 characters.')
 result={}
 for entry in value:
  if not isinstance(entry,dict):raise ValueError('Every character must be an object.')
  values=entry.get('values',entry)
  if not isinstance(values,dict):raise ValueError('Character values must be an object.')
  name=values.get('name')
  if not isinstance(name,str) or not name.strip():raise ValueError('Each character needs a non-empty name.')
  if len(name)>200:raise ValueError('Character names must be at most 200 characters.')
  if any(not isinstance(v,(str,int,float,bool,type(None))) for v in values.values()):raise ValueError('Character values must be simple text, numbers or booleans.')
  values={k:v.strip() if isinstance(v,str) else v for k,v in values.items() if k not in {'id','values'}}
  ident=hashlib.sha256(json.dumps([name.strip(),values.get('realm',''),values.get('region','')],ensure_ascii=False).encode()).hexdigest()[:20]
  result[ident]={'id':ident,'name':name.strip()+((' — '+str(values['realm'])) if values.get('realm') else ''),'values':values}
 return list(result.values())
