"""RFC 6901 field addressing; reviewed local Qwen draft with strict escape/index handling."""
import copy,re

def escape(key):return str(key).replace('~','~0').replace('/','~1')
def pointer_parts(pointer):
 if not isinstance(pointer,str) or pointer and not pointer.startswith('/'):raise ValueError('Invalid field pointer.')
 if pointer=='':return []
 parts=pointer[1:].split('/')
 if any(re.search(r'~(?![01])',part) for part in parts):raise ValueError('Invalid field-pointer escape.')
 return [part.replace('~1','/').replace('~0','~') for part in parts]

def pointer_get(root,pointer):
 current=root
 for key in pointer_parts(pointer):
  if isinstance(current,dict) and key in current:current=current[key]
  elif isinstance(current,list) and re.fullmatch(r'0|[1-9][0-9]*',key) and int(key)<len(current):current=current[int(key)]
  else:raise ValueError('The selected field does not exist.')
 return current

def apply_field(root,operation):
 path=operation['pointer'];parts=pointer_parts(path)
 if not parts:raise ValueError('Replace fields, not the entire corpus root.')
 parent=pointer_get(root,'/'+('/'.join(escape(k) for k in parts[:-1]))) if len(parts)>1 else root;key=parts[-1];kind=operation['operation']
 if isinstance(parent,list) and kind=='replace':
  if not re.fullmatch(r'0|[1-9][0-9]*',key) or int(key)>=len(parent):
   raise ValueError('The selected list item does not exist.')
  index=int(key)
  previous=parent[index]
  parent[index]=copy.deepcopy(operation.get('value'))
  return {'operation':'replace','pointer':path,'value':previous}
 if not isinstance(parent,dict):raise ValueError('Edit object fields; record order and array entries are preserved.')
 exists=key in parent
 if 'position' in operation and (type(operation['position']) is not int or not 0<=operation['position']<=len(parent)):raise ValueError('Invalid field position.')
 if kind=='rename':
  new=operation.get('new_key')
  if not isinstance(new,str) or not new or new in parent:raise ValueError('Choose a new, unused field name.')
  replacement={new if k==key else k:v for k,v in parent.items()}
  if not exists:raise ValueError('The selected field does not exist.')
  parent.clear();parent.update(replacement);return {'operation':'rename','pointer':'/'+('/'.join(escape(k) for k in [*parts[:-1],new])),'new_key':key}
 if kind not in {'add','replace','remove'}:raise ValueError('Unknown field edit.')
 if kind=='add' and exists:raise ValueError('That field already exists; use Change value.')
 if kind!='add' and not exists:raise ValueError('The selected field does not exist.')
 inverse={'operation':'remove' if kind=='add' else 'add' if kind=='remove' else 'replace','pointer':path}
 if exists:inverse.update(value=copy.deepcopy(parent[key]),position=list(parent).index(key))
 if kind=='remove':del parent[key]
 elif kind=='add' and 'position' in operation:
  items=list(parent.items());items.insert(min(operation['position'],len(items)),(key,copy.deepcopy(operation.get('value'))));parent.clear();parent.update(items)
 else:parent[key]=copy.deepcopy(operation.get('value'))
 return inverse

def mapped_pointer(row,path):
 if path in row:return '/'+escape(path)
 return '/'+('/'.join(escape(part) for part in path.split('.')))

def find_metadata_match(row, excluded, pattern):
 """Find the first literal match in an entry's visible field names or values."""
 def excerpt(text, match):
  start,end=match.span()
  return {'before':text[max(0,start-32):start], 'match':match[0], 'after':text[end:end+64]}

 def walk(value,pointer,caption):
  if pointer in excluded:return None
  field_match=pattern.search(caption) if caption else None
  if isinstance(value,(dict,list)):
   if field_match:
    return {'pointer':pointer, 'field':caption, 'in_field':True, **excerpt(caption,field_match)}
   children=value.items() if isinstance(value,dict) else enumerate(value)
   for key,child in children:
    if str(key).startswith(('_dmspeaks_', '_voxweave_')):continue
    label=f'{caption} / {key}' if caption else str(key)
    found=walk(child,pointer+'/'+escape(key),label)
    if found:return found
  else:
   text=value if isinstance(value,str) else ('null' if value is None else str(value))
   if match:=pattern.search(text):
    return {'pointer':pointer, 'field':caption, **excerpt(text,match)}
  if field_match:
   return {'pointer':pointer, 'field':caption, 'in_field':True, **excerpt(caption,field_match)}
  return None

 return walk(row,'','')
