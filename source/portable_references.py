"""Bundle corpus-declared local samples without changing the corpus or fetching URLs."""
import hashlib,json,re,urllib.parse,urllib.request,uuid
from pathlib import Path
from audio_sources import embedded

def declaration_hash(source):
 return hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()

def media_hash(path):
 digest=hashlib.sha256()
 with Path(path).open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
 return digest.hexdigest()

def local_path(app,dataset,source):
 """A restored copy is valid only for this dataset and this exact declaration."""
 cached=(app.store.setting('corpus_reference_media:'+dataset,{}) or {}).get(source['id'])
 if cached and cached.get('declaration_sha256')==declaration_hash(source):
  path=Path(cached.get('path',''))
  if not path.is_file():raise ValueError('The bundled corpus reference is missing. Restore the project or import the sample again.')
  if media_hash(path)!=cached.get('sha256'):raise ValueError('The bundled corpus reference has changed. Restore the project or import the sample again.')
  return path
 uri=source.get('uri','').strip()
 if '://' in uri:
  parsed=urllib.parse.urlsplit(uri)
  if parsed.scheme!='file' or parsed.netloc not in ('','localhost'):return None
  path=Path(urllib.request.url2pathname(parsed.path)).expanduser()
 else:path=Path(uri).expanduser()
 return path if path.is_absolute() and path.is_file() else None

def write_media(app,dataset,data,archive):
 """Return media bindings and explicit omissions. No network or TTS calls."""
 items=[];omitted=[];written={}
 for source in embedded(data).get('sources',[]):
  try:
   path=local_path(app,dataset,source)
   if path is None:raise ValueError('Not bundled: sample is remote, relative or unavailable on this computer. Its declaration is preserved.')
   if path.stat().st_size>30_000_000:raise ValueError('Reference exceeds the 30 MB sample limit.')
   raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
   entry=written.get(digest)
   if entry is None:
    from audio import probe
    probe(path)
    entry='references/corpus-'+digest+path.suffix.lower();archive.writestr(entry,raw);written[digest]=entry
   items.append({'source_id':source['id'],'declaration_sha256':declaration_hash(source),'sha256':digest,'media_entry':entry})
  except (ValueError,OSError) as exc:omitted.append({'source_id':source['id'],'reason':str(exc)})
 return items,omitted

def validate_media(manifest,parsed,namelist):
 """Validate all identities before import can modify the destination project."""
 items=manifest.get('corpus_reference_media',[])
 if not isinstance(items,list) or len(items)>10000:raise ValueError('Invalid bundled corpus references.')
 declarations={s['id']:s for s in embedded(parsed).get('sources',[])};seen=set()
 for item in items:
  if not isinstance(item,dict):raise ValueError('Invalid bundled corpus reference.')
  ident=item.get('source_id')
  if not isinstance(ident,str) or ident not in declarations or ident in seen:raise ValueError('Unknown or duplicate bundled corpus reference ID.')
  seen.add(ident)
  for key in ('sha256','declaration_sha256'):
   if not isinstance(item.get(key),str) or not re.fullmatch(r'[0-9a-f]{64}',item[key]):raise ValueError('Invalid bundled reference digest.')
  if item['declaration_sha256']!=declaration_hash(declarations[ident]):raise ValueError('Bundled reference does not match its corpus declaration.')
  entry=item.get('media_entry')
  if not isinstance(entry,str) or not entry.startswith('references/') or entry not in namelist:raise ValueError('Missing bundled corpus reference media.')
 return items

def restore_media(app,dataset,items,blobs):
 cache={};paths={};folder=app.folder/'references';folder.mkdir(exist_ok=True)
 for item in items:
  entry=item['media_entry']
  if entry not in paths:
   suffix=Path(entry).suffix.lower()
   if suffix not in {'.wav','.flac','.ogg','.mp3','.m4a','.aac','.aiff','.aif','.opus'}:suffix='.audio'
   path=folder/('corpus-'+uuid.uuid4().hex+suffix);path.write_bytes(blobs[entry]);paths[entry]=str(path)
  cache[item['source_id']]={'path':paths[entry],'sha256':item['sha256'],'declaration_sha256':item['declaration_sha256']}
 app.store.set_setting('corpus_reference_media:'+dataset,cache)
