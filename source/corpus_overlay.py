"""Validated working corpus snapshots; imported bytes stay immutable."""
import copy,json,re
from model import flatten,getfield

def working_view(original,root,mapping,revision):
 root=copy.deepcopy(root);container=original['container']
 if container is None:
  if not isinstance(root,list):raise ValueError('Keep the imported top-level record array.')
  rows=root
 else:
  if not isinstance(root,dict) or not isinstance(root.get(container),list):raise ValueError('Keep the imported record container.')
  rows=root[container]
 if len(rows)!=len(original['rows']) or any(not isinstance(row,dict) for row in rows):raise ValueError('Metadata edits must retain the record count and order.')
 if isinstance(root,dict) and root.get('format')=='open-speech-corpus':
  from osc_validator import validate_corpus
  errors=validate_corpus(root)
  if errors:raise ValueError('; '.join(errors[:12]))
 fields=list(dict.fromkeys(key for row in rows for key in flatten(row)))
 slots=sorted([key for key in fields if re.fullmatch(r'segment_\d+',key)],key=lambda key:int(key.split('_')[-1]))
 if not isinstance(mapping,dict) or any(not isinstance(key,str) or not isinstance(value,str) for key,value in mapping.items()):raise ValueError('Corpus mappings must contain text field paths.')
 if mapping.get('text')!='@segments' and mapping.get('text') not in fields:raise ValueError('The source-text mapping must remain valid.')
 if mapping.get('text')=='@segments' and not slots:raise ValueError('Keep at least one source segment column.')
 if any(value and value!='@segments' and value not in fields for value in mapping.values()):raise ValueError('A corpus mapping refers to a missing field.')
 # Field edits cannot silently repoint existing takes/drafts at another dialogue.
 for old,new in zip(original['rows'],rows):
  if getfield(old,original['mapping'].get('id'))!=getfield(new,mapping.get('id')):raise ValueError('Metadata edits must preserve stable dialogue IDs and record order.')
  def source(row,parsed):
   return ''.join(str(row.get(key) or '') for key in parsed['slots']) if parsed['mapping']['text']=='@segments' else getfield(row,parsed['mapping']['text'])
  if source(old,original)!=source(new,{'slots':slots,'mapping':mapping}):raise ValueError('Edit dialogue text as a draft, not inside the metadata snapshot.')
 if original['kind']=='csv' and any(isinstance(value,(dict,list)) for row in rows for value in row.values()):raise ValueError('CSV metadata values must be scalar.')
 json.dumps(root,ensure_ascii=False,allow_nan=False)
 parsed={**original,'root':root,'rows':rows,'fields':fields,'slots':slots,'mapping':copy.deepcopy(mapping),'overlay_revision':revision}
 from clip_group_corpus import read
 parsed['clip_group_config']=read(parsed)
 from corpus_lookup import read as read_lookup
 parsed['lookup']=read_lookup(parsed)
 return parsed
