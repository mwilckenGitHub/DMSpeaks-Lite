"""Validation for optional UI suggestion catalogs; these do not control inference."""
import json

def normalize_direction_tags(value):
 if value is None or isinstance(value,str) and not value.strip():return ''
 if isinstance(value,str):
  try:value=json.loads(value)
  except (ValueError,TypeError):raise ValueError('Direction suggestions must be a JSON array of strings.') from None
 if not isinstance(value,list) or len(value)>128:raise ValueError('Use at most 128 direction suggestion strings.')
 result=[];seen=set()
 for tag in value:
  if not isinstance(tag,str):raise ValueError('Direction suggestions must contain text only.')
  tag=tag.strip()
  if not tag or len(tag)>160 or any(c in tag for c in '[]\r\n'):raise ValueError('Each direction must be 1–160 characters, without brackets or line breaks.')
  key=tag.lower()
  if key not in seen:result.append(tag);seen.add(key)
 return json.dumps(result,ensure_ascii=False)
